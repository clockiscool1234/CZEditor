from __future__ import annotations

import sys

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox

from .backend import BackendError, LocalBackend
from .main_window import MainWindow
from .resources import icon_path


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("CZEditor")
    app.setWindowIcon(QIcon(icon_path()))

    try:
        backend = LocalBackend()
    except BackendError as e:
        QMessageBox.critical(None, "CZEditor", str(e))
        return 1

    window = MainWindow(backend)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
