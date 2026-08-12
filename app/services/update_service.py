from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from enum import auto, IntEnum
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QVersionNumber, Signal
from loguru import logger

from app.client import buildClient, fetchFile
from app.config.constants import VERSION
from app.config.paths import APP_DATA_DIR, executableDir

if TYPE_CHECKING:
    from app.services.coroutine_runner import CoroutineRunner

STAGING_DIR = Path(APP_DATA_DIR) / "update_staging"

def applyPendingPackUpdates(featuresDir: Path) -> None:
    if not featuresDir.exists():
        return
    for pending in featuresDir.glob("*_pending"):
        packId = pending.name.removesuffix("_pending")
        target = featuresDir / packId
        if target.exists():
            shutil.rmtree(target)
        pending.rename(target)
        logger.info("已应用 Pack 更新: {}", packId)


SOURCES = {
    "github": "https://github.com/XiaoYouChR/Ghost-Downloader-3/releases/download/v{version}/versions.json",
    "gitcode": "https://gitcode.com/XiaoYouChR/Ghost-Downloader-3/releases/download/v{version}/versions.json",
}


class UpdateState(IntEnum):
    IDLE = auto()
    CHECKING = auto()
    AVAILABLE = auto()
    DOWNLOADING = auto()
    READY = auto()
    FAILED = auto()


@dataclass(frozen=True)
class UpdateInfo:
    targetId: str
    label: str
    currentVersion: str
    latestVersion: str
    state: UpdateState = UpdateState.IDLE
    progress: float = 0
    error: str = ""


class UpdateService(QObject):
    changed = Signal(object)

    def __init__(self, coroutineRunner: CoroutineRunner, parent=None):
        super().__init__(parent)
        self._coroutineRunner = coroutineRunner
        self._infos: dict[str, UpdateInfo] = {}
        self._source = ""
        self._versionsData: dict = {}

    def infoById(self, targetId: str) -> UpdateInfo | None:
        return self._infos.get(targetId)

    def availableUpdates(self) -> list[UpdateInfo]:
        return [i for i in self._infos.values() if i.state in (UpdateState.AVAILABLE, UpdateState.READY)]

    def check(self) -> None:
        self._coroutineRunner.submit(self._check())

    def download(self, targetId: str) -> None:
        self._coroutineRunner.submit(self._download(targetId))

    def apply(self) -> None:
        for info in self._infos.values():
            if info.state == UpdateState.READY and info.targetId != "app":
                self._applyPack(info.targetId)

    def startUpdater(self) -> None:
        info = self._infos.get("app")
        if info is None or info.state != UpdateState.READY:
            return
        self._startUpdater()

    # ── Private ──

    async def _check(self) -> None:
        self._emit("app", UpdateState.CHECKING, label=f"Ghost Downloader {VERSION}")

        data = await self._fetchVersions()
        if data is None:
            self._emit("app", UpdateState.FAILED, error="无法获取版本信息")
            return
        self._versionsData = data

        appData = data.get("app", {})
        latestVersion = appData.get("version", "")
        if latestVersion and self._isNewer(VERSION, latestVersion):
            self._emit("app", UpdateState.AVAILABLE,
                        label=f"Ghost Downloader {latestVersion}",
                        latestVersion=latestVersion)

        packsData = data.get("packs", {})
        featuresDir = executableDir / "features"
        from app.services.pack_loader import PackManifest
        for packDir in sorted(featuresDir.iterdir()) if featuresDir.exists() else []:
            if not packDir.is_dir() or packDir.name.startswith("."):
                continue
            manifest = PackManifest.fromDir(packDir)
            if manifest is None or not manifest.version:
                continue
            remoteInfo = packsData.get(manifest.name)
            if remoteInfo is None:
                continue
            remoteVersion = remoteInfo.get("version", "")
            if remoteVersion and self._isNewer(manifest.version, remoteVersion):
                remoteGdMin = remoteInfo.get("gdMinVersion", "")
                if remoteGdMin and not self._isNewer(remoteGdMin, VERSION) and remoteGdMin != VERSION:
                    logger.debug("跳过 Pack 更新 {}：需要 GD ≥ {}", manifest.name, remoteGdMin)
                    continue
                self._emit(manifest.name, UpdateState.AVAILABLE,
                            label=f"{manifest.name} {remoteVersion}",
                            currentVersion=manifest.version,
                            latestVersion=remoteVersion)

    async def _fetchVersions(self) -> dict | None:
        sources = [self._source] if self._source else list(SOURCES.keys())
        for sourceName in sources:
            urlTemplate = SOURCES.get(sourceName)
            if not urlTemplate:
                continue
            try:
                url = await self._latestVersionsUrl(sourceName)
                if not url:
                    continue
                client = buildClient(timeout=15)
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    data = await response.json()
                    self._source = sourceName
                    return data
                finally:
                    client.close()
            except Exception as e:
                logger.debug("从 {} 获取版本信息失败: {}", sourceName, repr(e))
                continue
        return None

    async def _latestVersionsUrl(self, sourceName: str) -> str:
        client = buildClient(headers={"accept": "application/vnd.github+json"}, timeout=15)
        try:
            if sourceName == "github":
                resp = await client.get(
                    "https://api.github.com/repos/XiaoYouChR/Ghost-Downloader-3/releases/latest"
                )
                resp.raise_for_status()
                data = await resp.json()
                for asset in data.get("assets", []):
                    if asset.get("name") == "versions.json":
                        return asset.get("browser_download_url", "")
            elif sourceName == "gitcode":
                resp = await client.get(
                    "https://gitcode.com/api/v5/repos/XiaoYouChR/Ghost-Downloader-3/releases/latest"
                )
                resp.raise_for_status()
                data = await resp.json()
                for asset in data.get("assets", []):
                    if asset.get("name") == "versions.json":
                        return asset.get("browser_download_url", "")
        except Exception:
            pass
        finally:
            client.close()
        return ""

    async def _download(self, targetId: str) -> None:
        info = self._infos.get(targetId)
        if info is None or info.state != UpdateState.AVAILABLE:
            return

        self._emit(targetId, UpdateState.DOWNLOADING)
        STAGING_DIR.mkdir(parents=True, exist_ok=True)

        try:
            if targetId == "app":
                await self._downloadAppPatch(info)
            else:
                await self._downloadPack(targetId, info)
        except Exception as e:
            logger.opt(exception=e).error("下载更新失败: {}", targetId)
            self._emit(targetId, UpdateState.FAILED, error=str(e))

    async def _downloadAppPatch(self, info: UpdateInfo) -> None:
        appData = self._versionsData.get("app", {})
        patchFrom = appData.get("patchFrom", "")

        if patchFrom == VERSION:
            url = self._assetUrl(f"patch-{patchFrom}-to-{info.latestVersion}.hdiff")
            outputPath = STAGING_DIR / "patch.hdiff"
        else:
            url = self._bestFullReleaseUrl()
            if not url:
                self._emit("app", UpdateState.FAILED, error="未找到适配的安装包")
                return
            outputPath = STAGING_DIR / "full_release"

        await fetchFile(url, outputPath, onProgress=lambda p: self._emit("app", UpdateState.DOWNLOADING, progress=p))

        expectedSha = appData.get("patchSha256" if patchFrom == VERSION else "fullSha256", "")
        if expectedSha:
            from app.platform.filesystem import matchChecksum
            if not matchChecksum(outputPath, expectedSha):
                outputPath.unlink(missing_ok=True)
                self._emit("app", UpdateState.FAILED, error="校验失败")
                return

        self._emit("app", UpdateState.READY)

    async def _downloadPack(self, packId: str, info: UpdateInfo) -> None:
        packData = self._versionsData.get("packs", {}).get(packId, {})
        url = self._assetUrl(f"{packId}-{info.latestVersion}.zip")
        outputPath = STAGING_DIR / f"{packId}.zip"

        await fetchFile(url, outputPath, onProgress=lambda p: self._emit(packId, UpdateState.DOWNLOADING, progress=p))

        expectedSha = packData.get("sha256", "")
        if expectedSha:
            from app.platform.filesystem import matchChecksum
            if not matchChecksum(outputPath, expectedSha):
                outputPath.unlink(missing_ok=True)
                self._emit(packId, UpdateState.FAILED, error="校验失败")
                return

        self._emit(packId, UpdateState.READY)

    def _applyPack(self, packId: str) -> None:
        zipPath = STAGING_DIR / f"{packId}.zip"
        if not zipPath.is_file():
            return
        pendingDir = executableDir / "features" / f"{packId}_pending"
        pendingDir.mkdir(parents=True, exist_ok=True)
        import zipfile
        with zipfile.ZipFile(zipPath) as zf:
            zf.extractall(pendingDir)
        zipPath.unlink()
        logger.info("Pack 更新已暂存: {}", packId)

    def _startUpdater(self) -> None:
        patchPath = STAGING_DIR / "patch.hdiff"
        if not patchPath.is_file():
            return

        updaterName = "updater.exe" if sys.platform == "win32" else "updater"
        updaterPath = executableDir / updaterName
        if not updaterPath.is_file():
            logger.error("updater not found: {}", updaterPath)
            return

        if sys.platform == "darwin":
            appDir = executableDir.parent.parent
        else:
            appDir = executableDir

        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [str(updaterPath), str(os.getpid()), str(appDir),
             str(patchPath), sys.executable],
            **kwargs,
        )
        logger.info("updater started")

    # ── Helpers ──

    def _assetUrl(self, assetName: str) -> str:
        appData = self._versionsData.get("app", {})
        version = appData.get("version", "")
        if self._source == "gitcode":
            return f"https://gitcode.com/XiaoYouChR/Ghost-Downloader-3/releases/download/v{version}/{assetName}"
        return f"https://github.com/XiaoYouChR/Ghost-Downloader-3/releases/download/v{version}/{assetName}"

    def _bestFullReleaseUrl(self) -> str:
        from app.update import fetchRelease, bestAsset
        # full release fallback 走已有的 update.py 逻辑选择最佳资产
        # 这里只返回 URL，实际大文件下载可以走 TaskService
        return ""

    def _isNewer(self, current: str, latest: str) -> bool:
        v1 = QVersionNumber.fromString(current.lstrip("vV"))
        v2 = QVersionNumber.fromString(latest.lstrip("vV"))
        return v2 > v1

    def _emit(self, targetId: str, state: UpdateState, **kwargs) -> None:
        current = self._infos.get(targetId)
        if current is None:
            info = UpdateInfo(
                targetId=targetId,
                label=kwargs.get("label", targetId),
                currentVersion=kwargs.get("currentVersion", VERSION if targetId == "app" else ""),
                latestVersion=kwargs.get("latestVersion", ""),
                state=state,
                progress=kwargs.get("progress", 0),
                error=kwargs.get("error", ""),
            )
        else:
            info = replace(current, state=state, **{
                k: v for k, v in kwargs.items()
                if k in ("label", "currentVersion", "latestVersion", "progress", "error")
            })
        self._infos[targetId] = info
        self.changed.emit(info)
