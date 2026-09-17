"""PyInstaller 打包入口。

包内 ``__main__.py`` 用相对导入（``python -m writerstudio``），PyInstaller
把脚本当独立模块编译时相对导入会炸——经包成员身份调用即可。
"""

from writerstudio.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
