# 部署脚本的 YAML 读取依赖

`pyyaml-6.0.3.zip` 随仓库分发，Python 标准库可直接从压缩包导入代码。首次部署时即可读取配置，即使宿主机没有安装项目依赖或无法访问 PyPI。

包内保留 PyYAML 6.0.3 的 MIT 许可证。读取模块的源码逐字节取自官方 `pyyaml-6.0.3.tar.gz` 的 `lib/yaml/`，源码包 SHA-256 为 `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`。[PyPI 源码包](https://pypi.org/project/PyYAML/6.0.3/#files)

压缩包在私有包目录 `_mycomfy_yaml/` 下保存空的 `__init__.py` 及 `composer`、`constructor`、`error`、`events`、`loader`、`nodes`、`parser`、`reader`、`resolver`、`scanner`、`tokens` 十一个上游 Python 模块，许可证放在压缩包根目录。部署端使用其中的 `SafeLoader`。
