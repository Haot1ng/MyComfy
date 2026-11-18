"""兼容原有 python -m mycomfy.deploy 命令。"""

from mycomfy.engine.deploy import main

if __name__ == "__main__":
    main()
