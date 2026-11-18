"""兼容原有 python -m mycomfy.runtime 命令。"""

from mycomfy.engine.runtime import main

if __name__ == "__main__":
    main()
