# MarkitForge

> [!TIP]
> MarkitForge is a Python package and command-line utility for converting various files to Markdown (e.g., for indexing, text analysis, etc).
>
> For more information, and full documentation, see the project [README.md](https://github.com/Marcel-Bonifacio/MarkitForge) on GitHub. MarkitForge is a fork of [microsoft/markitdown](https://github.com/microsoft/markitdown).

> [!IMPORTANT]
> MarkitForge performs I/O with the privileges of the current process. Like open() or requests.get(), it will access resources that the process itself can access. Sanitize your inputs in untrusted environments, and call the narrowest `convert_*` function needed for your use case (e.g., `convert_stream()`, or `convert_local()`). See the [Security Considerations](https://github.com/Marcel-Bonifacio/MarkitForge#security-considerations) section of the documentation for more information.

## Installation

Not on PyPI yet (don't `pip install markitforge` from PyPI: the name is unclaimed). From source:

```bash
git clone https://github.com/Marcel-Bonifacio/MarkitForge.git
cd MarkitForge
pip install -e 'packages/markitforge[all]'
```

## Usage

### Command-Line

```bash
markitforge path-to-file.pdf > document.md
```

### Python API

```python
from markitforge import MarkitForge

md = MarkitForge()
result = md.convert("test.xlsx")
print(result.markdown)
```

### More Information

For more information, and full documentation, see the project [README.md](https://github.com/Marcel-Bonifacio/MarkitForge) on GitHub. MarkitForge is a fork of [microsoft/markitdown](https://github.com/microsoft/markitdown).

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.

## License

MarkItDown code: MIT, Copyright (c) Microsoft Corporation (`LICENSE`). New MarkitForge code and changes: Apache License 2.0 (`LICENSE-APACHE`, `NOTICE`). See `ThirdPartyNotices.md` for dwml (Apache-2.0).
