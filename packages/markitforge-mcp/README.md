# MarkitForge-MCP

> Part of [MarkitForge](https://github.com/Marcel-Bonifacio/MarkitForge), a fork of [MarkItDown](https://github.com/microsoft/markitdown) (MIT, Copyright (c) Microsoft Corporation). This package is derived from MarkItDown's MCP server; MarkItDown parts are MIT (`LICENSE`), MarkitForge changes are Apache-2.0 (`LICENSE-APACHE`, `NOTICE`).

> [!IMPORTANT]
> The MarkitForge-MCP package is meant for **local use**, with local trusted agents. In particular, when running the MCP server with Streamable HTTP or SSE, it binds to `localhost` by default, and is not exposed to other machines on the network or Internet. In this configuration, it is meant to be a direct alternative to the STDIO transport, which may be more convenient in some cases. DO NOT bind the server to other interfaces unless you understand the [security implications](#security-considerations) of doing so.


[![PyPI](https://img.shields.io/pypi/v/markitforge-mcp.svg)](https://pypi.org/project/markitforge-mcp/)
![PyPI - Downloads](https://img.shields.io/pypi/dd/markitforge-mcp)
[![Built by AutoGen Team](https://img.shields.io/badge/Built%20by-AutoGen%20Team-blue)](https://github.com/microsoft/autogen)

The `markitforge-mcp` package provides a lightweight STDIO, Streamable HTTP, and SSE MCP server for calling MarkitForge.

It exposes one tool: `convert_to_markdown(uri)`, where uri can be any `http:`, `https:`, `file:`, or `data:` URI.

## Installation

To install the package, use pip:

```bash
pip install -e 'packages/markitforge[all]' -e packages/markitforge-mcp  # from the repo checkout; not on PyPI yet
```

## Usage

To run the MCP server, using STDIO (default), use the following command:


```bash
markitforge-mcp
```

To run the MCP server, using Streamable HTTP and SSE, use the following command:

```bash
markitforge-mcp --http --host 127.0.0.1 --port 3001
```

## Running in Docker

To run `markitforge-mcp` in Docker, build the Docker image from the repository root (so the core package comes from this checkout):
```bash
docker build -f packages/markitforge-mcp/Dockerfile -t markitforge-mcp:latest .
```

And run it using:
```bash
docker run -it --rm markitforge-mcp:latest
```
This will be sufficient for remote URIs. To access local files, you need to mount the local directory into the container. For example, if you want to access files in `/home/user/data`, you can run:

```bash
docker run -it --rm -v /home/user/data:/workdir markitforge-mcp:latest
```

Once mounted, all files under data will be accessible under `/workdir` in the container. For example, if you have a file `example.txt` in `/home/user/data`, it will be accessible in the container at `/workdir/example.txt`.

## Accessing from Claude Desktop

It is recommended to use the Docker image when running the MCP server for Claude Desktop.

Follow [these instructions](https://modelcontextprotocol.io/quickstart/user#for-claude-desktop-users) to access Claude's `claude_desktop_config.json` file.

Edit it to include the following JSON entry:

```json
{
  "mcpServers": {
    "markitforge": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "markitforge-mcp:latest"
      ]
    }
  }
}
```

If you want to mount a directory, adjust it accordingly:

```json
{
  "mcpServers": {
    "markitforge": {
      "command": "docker",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v",
        "/home/user/data:/workdir",
        "markitforge-mcp:latest"
      ]
    }
  }
}
```

## Debugging

To debug the MCP server you can use the `MCP Inspector` tool.

```bash
npx @modelcontextprotocol/inspector
```

You can then connect to the inspector through the specified host and port (e.g., `http://localhost:5173/`).

If using STDIO:
* select `STDIO` as the transport type,
* input `markitforge-mcp` as the command, and
* click `Connect`

If using Streamable HTTP:
* select `Streamable HTTP` as the transport type,
* input `http://127.0.0.1:3001/mcp` as the URL, and
* click `Connect`

If using SSE:
* select `SSE` as the transport type,
* input `http://127.0.0.1:3001/sse` as the URL, and
* click `Connect`

Finally:
* click the `Tools` tab,
* click `List Tools`,
* click `convert_to_markdown`, and
* run the tool on any valid URI.

## Security Considerations

The server has no authentication and runs with the privileges of the user running it. Over SSE / Streamable HTTP it binds to `localhost` by default, but any process or user on the same machine can still reach it. DO NOT bind it to other interfaces unless you understand the risk.

MarkitForge adds these guards:

| Guard | stdio (default) | HTTP / SSE (default) | Change with |
|---|---|---|---|
| `file:` URIs | allowed anywhere | **off** | `--allow-file-root DIR` (repeatable) or `MARKITFORGE_MCP_FILE_ROOTS`; `--no-file-access` |
| URLs on private / loopback / link-local / cloud-metadata addresses (also checked on every redirect hop) | allowed | **blocked** | `--block-private-urls` (stdio), `--allow-private-urls` (HTTP), or `MARKITFORGE_BLOCK_PRIVATE_URLS=1` |
| Download limits | 512 MB, 60 s read timeout, 300 s total, 10 redirects | same | library options |
| `convert_to_file` | writes only new files (no overwrite, no symlink following, mode 0600) into a private per-user folder | same | `MARKITFORGE_MCP_OUTPUT_DIR` |

An agent that reads untrusted documents can be prompt-injected into fetching URLs or reading files. Over stdio, consider `--allow-file-root` and `--block-private-urls`, or run the server in a sandbox (container or VM) that limits files and network access.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
