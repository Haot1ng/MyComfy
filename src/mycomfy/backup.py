"""兼容原有 python -m mycomfy.backup 命令。"""

from mycomfy.ops.backup import main

if __name__ == "__main__":
    main()
