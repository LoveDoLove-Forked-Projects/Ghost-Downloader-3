# coding:utf-8
from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QEvent
from PySide6.QtGui import QColor, QResizeEvent
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QWidget,
    QFrame,
)
from qfluentwidgets import ScrollArea, isDarkTheme


class MaskDialogBase(QDialog):
    """Dialog box base class with a mask"""

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._isClosableOnMaskClicked = False
        self._hBoxLayout = QHBoxLayout(self)
        self.windowMask = QWidget(self)

        # dialog box in the center of mask, all widgets take it as parent
        self.widget = ScrollArea(self)
        self.widget.setObjectName("centerWidget")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(0, 0, parent.width(), parent.height())

        c = 0 if isDarkTheme() else 255
        self.windowMask.resize(self.size())
        self.windowMask.setStyleSheet(f"background:rgba({c}, {c}, {c}, 0.6)")
        self._hBoxLayout.addWidget(self.widget)
        self.setShadowEffect()

        self.window().installEventFilter(self)
        self.windowMask.installEventFilter(self)

    def setShadowEffect(
        self, blurRadius=60, offset=(0, 10), color=QColor(0, 0, 0, 100)
    ):
        """add shadow to dialog"""
        shadowEffect = QGraphicsDropShadowEffect(self.widget)
        shadowEffect.setBlurRadius(blurRadius)
        shadowEffect.setOffset(*offset)
        shadowEffect.setColor(color)
        self.widget.setGraphicsEffect(None)
        self.widget.setGraphicsEffect(shadowEffect)

    def setMaskColor(self, color: QColor):
        """set the color of mask"""
        self.windowMask.setStyleSheet(
            f"""
            background: rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})
        """
        )

    def showEvent(self, e):
        """fade in"""
        opacityEffect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(opacityEffect)
        opacityAni = QPropertyAnimation(opacityEffect, b"opacity", self)
        opacityAni.setStartValue(0)
        opacityAni.setEndValue(1)
        opacityAni.setDuration(200)
        opacityAni.setEasingCurve(QEasingCurve.InSine)
        opacityAni.finished.connect(lambda: self.setGraphicsEffect(None))
        opacityAni.start()
        super().showEvent(e)

    def done(self, code):
        """fade out"""
        self.widget.setGraphicsEffect(None)
        opacityEffect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(opacityEffect)
        opacityAni = QPropertyAnimation(opacityEffect, b"opacity", self)
        opacityAni.setStartValue(1)
        opacityAni.setEndValue(0)
        opacityAni.setDuration(100)
        opacityAni.finished.connect(lambda: self._onDone(code))
        opacityAni.finished.connect(opacityAni.deleteLater)
        opacityAni.start()

    def _onDone(self, code):
        self.setGraphicsEffect(None)
        QDialog.done(self, code)
        self.deleteLater()

    def isClosableOnMaskClicked(self):
        return self._isClosableOnMaskClicked

    def setClosableOnMaskClicked(self, isClosable: bool):
        self._isClosableOnMaskClicked = isClosable

    def resizeEvent(self, e):
        self.windowMask.resize(self.size())

    def eventFilter(self, obj, e: QEvent):
        if obj is self.window():
            if e.type() == QEvent.Resize:
                re = QResizeEvent(e)
                self.resize(re.size())
        elif obj is self.windowMask:
            if (
                e.type() == QEvent.MouseButtonRelease
                and e.button() == Qt.LeftButton
                and self.isClosableOnMaskClicked()
            ):
                self.reject()

        return super().eventFilter(obj, e)
