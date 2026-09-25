# Contributing to DocVM

感谢关注 DocVM。欢迎 Issue 与 Pull Request。

## 开发环境

```bash
git clone https://github.com/yuJunOk/doc-version-manager.git
cd doc-version-manager
pip install -r requirements.txt
python main.py
```

浏览器打开 `http://127.0.0.1:404`。数据目录为项目根下的 `.docvm/`（已 gitignore）。

## 提交建议

- 一次 PR 只做一件事（修 bug / 加功能 / 改文档）
- 提交说明写清「为什么」，避免无意义的 `update`
- 不要提交 `.docvm/`、`dist/`、`build/`、本机路径或密钥
- UI 改动请附简要说明或截图

## 代码结构

详见 [docs/技术文档.md](docs/技术文档.md)。核心包在 `docvm/`，入口为 `main.py`。

## 行为准则

请保持友善、就事论事。恶意内容、人身攻击或明显恶意代码贡献将被拒绝。

## License

贡献内容默认按仓库 [MIT License](LICENSE) 授权。
