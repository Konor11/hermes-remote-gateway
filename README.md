# Hermes Remote Gateway

Подключает **нативный CLI/TUI Hermes на ноуте** к **удалённому Hermes gateway** (серверу). После `hermes remote connect` обычная команда `hermes` открывает полноценный нативный интерфейс (скины, сессии, стриминг, approval) — но весь рантайм (tools, sessions, memory, browser, MCP) работает на удалённом сервере.

## Как это работает

```
hermes remote connect
  → пишет HERMES_TUI_GATEWAY_URL=ws://127.0.0.1:43827/api/ws в ~/.hermes/.env
  → стартует локальный WebSocket-прокси (daemon) на 127.0.0.1:43827

hermes                  ← ОБЫЧНАЯ команда, без подкоманды
  → читает .env → нативный TUI подключается к ws://127.0.0.1:43827
  → прокси минтит свежий WS-ticket и туннелирует в wss://dash.dktunnel.xyz/api/ws
  → ОТКРЫВАЕТСЯ НАТИВНЫЙ TUI удалённого gateway

hermes remote disconnect
  → .env очищается, daemon гасится
  → hermes снова локальный
```

**Hermes не знает о remote** — просто считает, что у него локальный gateway на `127.0.0.1`. Поэтому интерфейс «как локальный» без доработок.

## Установка

Склонируй плагин в директорию плагинов Hermes:

```bash
git clone https://github.com/Konor11/hermes-remote-gateway.git \
  ~/.hermes/plugins/hermes-remote-gateway

hermes plugins enable hermes-remote-gateway
```

Проверь, что команда появилась:

```bash
hermes remote --help
```

## Настройка

```bash
# URL удалённого gateway
hermes config set remote_gateway.url "https://dash.dktunnel.xyz"

# Режим аутентификации: oauth | token | basic
hermes config set remote_gateway.auth "oauth"

# Если basic:
hermes config set remote_gateway.username "admin"
hermes config set remote_gateway.password "твой-пароль"

# Если token:
hermes config set remote_gateway.token "session-token"
```

## Использование

```bash
# Подключиться к удалённому gateway (нативный TUI через .env)
hermes remote connect

# Обычный hermes — откроется подключённый TUI
hermes

# Вернуться к локальному Hermes
hermes remote disconnect

# Мгновенные команды
hermes remote chat -q "привет"      # один запрос
hermes remote status                 # статус
hermes remote config                 # показать конфиг
```

## Аутентификация

| Режим | Как работает | Когда |
|-------|--------------|-------|
| `oauth` | Native PKCE (RFC 8252): браузер → Nous Portal → токен | интернет, recommended |
| `token` | Статический session token из дашборда | доверенная среда |
| `basic` | Логин/пароль через `/auth/password-login` + WS-ticket | LAN / VPN / Tailscale |

Прокси сам минтит свежий WS-ticket при каждом подключении TUI, поэтому одноразовый ticket (TTL ~30с) не мешает — Hermes всегда подключается к локальному прокси, а прокси живёт и переминтит tickets.

## Файлы

| Файл | Роль |
|------|------|
| `auth.py` | OAuth/Token/Basic auth, минтинг WS-ticket |
| `daemon.py` | Локальный WebSocket-прокси к удалённому gateway |
| `commands.py` | CLI: `remote connect/chat/status/disconnect/config` |
| `config.py` | Конфиг `remote_gateway` |
| `client.py` | Прямой WebSocket-клиент (для `chat`) |
| `protocol.py` | JSON-RPC/WebSocket типы сообщений |
| `__init__.py` | Регистрация плагина (`register(ctx)`) |

## Требования

- Hermes Agent (установленный и рабочий)
- `aiohttp` (`pip install aiohttp` или через `uv`)
- Network-доступ до удалённого gateway

## Лицензия

MIT