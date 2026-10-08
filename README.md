<p align="center"><img src="app/design/icons/prism-128.png" width="80" height="80" alt="Prism logo"></p>

# Prism

**One local model gateway. Multiple sources. Your coding agents.**

A Windows desktop console for model sources, routing and coding-agent connections, powered by [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI).

English · **[中文](README_CN.md)**

**[Download for Windows](https://github.com/glyahh/Agent-router-Pro-Max/releases/latest)** · [Release notes](https://github.com/glyahh/Agent-router-Pro-Max/releases) · [Report an issue](https://github.com/glyahh/Agent-router-Pro-Max/issues)

![Prism routing view with OpenAI and DeepSeek](docs/images/prism-home-demo.jpg)

*Current interface with official vendor names and demo configuration. No personal accounts, credentials or usage statistics; no live connection is implied.*

## Why Prism?

Using several AI services shouldn't mean editing several files every time you change a model source.

- **Sources and models in one place.** Organize groups, edit sources and choose which models to expose.
- **Identical names, distinct sources.** Give each source an identifier so the same model from two services remains distinguishable in your coding agent.
- **Preview before connecting.** Review configuration changes, then confirm. Connection changes are backed up before writing; disconnect only reverts Prism-managed fields that still match its written values.
- **Gateway control and request visibility.** Start or stop the gateway; check request charts, source status and logs.

Configuration adapters are available for **Codex, Claude Code, opencode and Hermes**. Model compatibility depends on the source protocol and coding-agent version. Copilot connection entries use direct-source configuration rather than the local gateway.

## Get started

**Requirements:** Windows 10/11 x64 and [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/).

1. Download the Windows ZIP from [the latest release](https://github.com/glyahh/Agent-router-Pro-Max/releases/latest).
2. Extract the entire ZIP into a writable folder. Keep `Prism.exe`, `_internal/` and `cli-proxy-api.exe` together. Prefer a path without spaces.
3. Run `Prism.exe`.
4. Add a source using your existing credentials, choose models and save routing.
5. Preview the connection changes for your coding agent, then confirm. Restart the agent if its model list does not refresh.

Prism is a control console, not a model service. You need access to the selected AI service; its usage charges and limits still apply.

## Same model, two sources

Keep one source as the main source and give the other an identifier such as `secondary`:

```text
Main source      GPT-6.1 Sol
Second source    secondary/GPT-6.1 Sol
```

Both can appear in your coding agent's model list. You choose the source explicitly; this is not automatic failover.

## Under the hood

Prism uses Python + WebView2 with native HTML/CSS/JavaScript. It manages routing and connections; **CLIProxyAPI handles inference traffic**, not Prism's console HTTP server. Default local ports: gateway `8317`, console `8318`.

Credentials are stored locally in files such as `config.yaml` and `.local-secrets.json`. Do not share these files or credential-bearing backups. Redact credentials, account details and private endpoints in screenshots or logs.

## Feedback & contributions

[Open an issue](https://github.com/glyahh/Agent-router-Pro-Max/issues) with your Prism version, Windows version, coding agent, reproduction steps and expected versus actual behavior. Redacted screenshots and logs help.

Focused fixes are welcome. Discuss larger changes in an issue first.

## License & acknowledgements

Prism is [MIT licensed](LICENSE). The bundled gateway is [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), an independent upstream project. Third-party components retain their own licenses; see [Third-party notices](THIRD-PARTY-NOTICES.md).
