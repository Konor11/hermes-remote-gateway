# Hermes Remote Gateway

Подключает **нативный CLI/TUI Hermes на ноуте** к **удалённому Hermes gateway** (серверу). После `hermes remote connect` обычная команда `hermes` открывает полноценный нативный интерфейс (скины, сессии, стриминг, approval), а весь рантайм (tools, sessions, memory, browser, MCP) работает на удалённом сервере.

Умеет также **отдать удалённому агенту файлы и команды этого ноутa**: терминальные и файловые инструменты начинают исполняться на ноуте по обратному SSH-туннелю.

## Как это работает

```
hermes remote connect
  → пишет HERMES_TUI_GATEWAY_URL=ws://127.0.0.1:43827/api/ws в ~/.hermes/.env
  → ставит и включает systemd --user unit с локальным WebSocket-прокси
    (переживает выход из системы и перезагрузку)
  → ставит display.interface: tui (чтобы bare `hermes` шёл TUI-путём)

hermes                  ← ОБЫЧНАЯ команда, без подкоманды
  → читает .env → нативный TUI подключается к ws://127.0.0.1:43827
  → прокси минтит свежий WS-ticket и туннелирует в wss://<домен>/api/ws
  → ОТКРЫВАЕТСЯ НАТИВНЫЙ TUI удалённого gateway

hermes remote disconnect
  → гасит и удаляет unit, чистит .env
  → hermes снова локальный
```

**Почему нужен локальный прокси, а не подключение прямо к домену.** Публичный (gated)
дашборд принимает на `/api/ws` только **одноразовый ticket** (TTL ~30 с) — статический
`?token=` в этом режиме запрещён ядром Hermes (`hermes_cli/web_server_chat.py`: *«a leaked
`_SESSION_TOKEN` must not grant access»*). Hermes Desktop умеет минтить ticket сам, а
`ui-tui` открывает голый WebSocket без заголовков и не умеет. Поэтому прокси держит сессию
и переминчивает tickets; протокол и авторизация остаются доменными (OAuth/Basic).

**Hermes не знает о remote** — считает, что у него локальный gateway на `127.0.0.1`. Поэтому интерфейс «как локальный» без доработок самого Hermes.

## Установка

Одна команда — сама склонирует плагин, поставит зависимости, спросит адрес и подключится:

```bash
git clone https://github.com/Konor11/hermes-remote-gateway.git /tmp/hrg && bash /tmp/hrg/setup.sh
rm -rf /tmp/hrg
```

Скрипт спросит только URL и способ входа. Несколько шагов вручную — только если нужно явнее:

```bash
git clone https://github.com/Konor11/hermes-remote-gateway.git \
  ~/.hermes/plugins/hermes-remote-gateway

cd ~/.hermes/plugins/hermes-remote-gateway
bash install.sh                 # системные зависимости: sshd + sshpass

hermes plugins enable hermes-remote-gateway
hermes remote --help
```

Полезно знать:

```bash
bash setup.sh --check                        # что уже установлено и работает (ничего не меняет)
bash setup.sh --url https://… --auth oauth   # без вопросов
bash setup.sh --local-pc --server-password '…'  # сразу с доступом к файлам ноута
```

> **Ставьте клоном, а не `hermes plugins install`.** Инсталлятор Hermes гоняет плагин через
> сканер безопасности, а этот плагин по своей сути попадает под правила `ssh_backdoor` и
> `sudo_usage` (он добавляет ключ в `~/.ssh/authorized_keys` и ставит системные пакеты).
> При вердикте *dangerous* установка **блокируется безусловно**. Путь `git clone` +
> `hermes plugins enable` сканер не проходит и работает.
>
> Если всё же нужен `hermes plugins install` — сначала отключите скан:
> `hermes config set plugins.scan_on_install false` (и верните потом `true`).

`install.sh` определяет дистрибутив по `/etc/os-release` (`apt-get`, `dnf`, `yum`, `pacman`,
`zypper`, `apk`, `pkg`, `brew`), сопоставляет имена пакетов, ставит **только отсутствующее**
через `sudo` и перепроверяет результат. Те же проверки выполняются автоматически при
`hermes remote pc setup` и при `hermes remote connect` в режиме local-PC, так что отдельно
запускать `install.sh` не обязательно.

## Настройка

```bash
hermes config set remote_gateway.url "https://mydomen.com"   # адрес вашего gateway
hermes config set remote_gateway.auth "oauth"                # oauth | token | basic

# если basic:
hermes config set remote_gateway.username "login"
hermes config set remote_gateway.password "пароль"

# если token (только для дашборда без gated-режима):
hermes config set remote_gateway.token "session-token"
```

## Использование

```bash
hermes remote connect        # подключиться (нативный TUI через .env + автозапуск прокси)
hermes                       # обычная команда — откроется удалённый TUI
hermes remote disconnect     # вернуться к локальному Hermes

hermes remote chat -q "привет"   # один запрос
hermes remote status             # статус (проверяет реальные .env/прокси/порт)
hermes remote config             # показать конфиг
```

## Доступ к файлам ноутa (local-PC)

Удалённый агент начинает исполнять терминальные и файловые инструменты **на этом ноуте**.

```bash
hermes config set remote_gateway.local_pc_access true

# один раз: ключи, обратный туннель, проверка цепочки, конфиг сервера
hermes remote pc setup --server-password '<root-пароль сервера>'

hermes remote connect        # профиль local-PC подставится автоматически
hermes
```

`--server-password` — пароль `root` **на удалённом сервере**; нужен один раз, чтобы залить туда
ключ обратного туннеля. Не сохраняется. **Адрес сервера отдельно не вводится**: он берётся из
`remote_gateway.url` (отбрасываются схема/путь/порт) — `pc setup` печатает цель SSH и
разрешённый IP.

Что делает `pc setup` (по шагам, с остановкой при любой ошибке — конфиг сервера не меняется,
пока цепочка не подтверждена):

1. зависимости (`sshd`, `sshpass`) — с определением системы;
2. проверяет, что `sshd` на ноуте слушает;
3. создаёт ключ туннеля и ставит его на сервер;
4. создаёт ключ «сервер → ноут» и авторизует его здесь;
5. поднимает обратный туннель `ноут:22 ← сервер:127.0.0.1:2222`;
6. проверяет цепочку `сервер → туннель → ноут` (`TUNNEL_OK`);
7. прописывает `terminal.backend: ssh` + `TERMINAL_*` в **отдельный профиль** `laptop`.

**Корневой конфиг сервера не трогается.** Настройки уходят в профиль `laptop`
(`~/.hermes/profiles/laptop/{config.yaml,.env}`), поэтому другие профили и Telegram-боты на
`default` не затронуты. При `local_pc_access: true` профиль `laptop` подставляется
автоматически, если не задан другой.

```bash
hermes remote pc status      # sshd/туннель/ключи/порт
hermes remote pc deps        # только зависимости
hermes remote pc on|off      # поднять/остановить туннель
```

### Откат

**Обычный (с ноута)** — возвращает агента на сервер:

```bash
hermes remote pc off                                   # остановить обратный туннель
hermes config set remote_gateway.local_pc_access false  # снять авто-подстановку профиля
```

Корневой конфиг сервера при этом не трогается — он как был `terminal.backend: "local"`, так и
остался: `ssh` прописывается **только** в профиль `laptop`.

**Полная уборка на сервере** (если профиль больше не нужен):

```bash
HERMES_HOME=$HOME/.hermes/profiles/laptop hermes config set terminal.backend local
hermes profile delete laptop
```

> Важно: `hermes config set terminal.backend local` **без** `HERMES_HOME=...` для этого плагина
> ничего не откатывает — в корневом конфиге никогда не было `ssh`.

**Вернуть обратно:** `hermes remote pc setup` — идемпотентна, повторит все шаги (зависимости,
ключи, туннель, конфиг профиля).

## Аутентификация

| Режим | Как работает | Когда |
|-------|--------------|-------|
| `oauth` | Native PKCE (RFC 8252): браузер → Nous Portal → токен | интернет, рекомендуется |
| `basic` | Логин/пароль через `/auth/password-login` + WS-ticket | LAN / VPN / Tailscale |
| `token` | Статический session token | только дашборд **без** gated-режима (на публичном отклоняется) |

Прокси минтит свежий WS-ticket на каждое подключение TUI, поэтому одноразовость ticket
(TTL ~30 с) не мешает и реконнект не ломается.

## Требования

- Hermes Agent (установленный и рабочий)
- `aiohttp` (зависимость плагина; Hermes ставит её при установке/включении плагина)
- Сетевой доступ до удалённого gateway
- Для local-PC: `sshd` и `sshpass` на ноуте — ставятся автоматически (`install.sh`,
  `pc setup`, `connect`); `systemd --user` для автозапуска прокси

## Диагностика

```bash
cat ~/.hermes/remote-gateway-daemon.log    # лог локального прокси
hermes remote status                       # .env / процесс / порт / профиль
systemctl --user status hermes-remote-gateway.service
```

## Файлы

| Файл | Роль |
|------|------|
| `auth.py` | OAuth/Token/Basic auth, минтинг WS-ticket |
| `daemon.py` | Локальный WebSocket-прокси: минтит ticket, форвардит пинги, подставляет профиль |
| `commands.py` | CLI: `remote connect/disconnect/status/chat/config/pc` |
| `localpc.py` | Обратный SSH-туннель, ключи, проверка цепочки, конфиг профиля `laptop` |
| `deps.py` | Определение ОС + установка системных зависимостей (`sshd`, `sshpass`) |
| `install.sh` | Установка зависимостей в один шаг |
| `config.py` | Конфиг `remote_gateway` |
| `client.py` | Прямой WebSocket-клиент (для `chat`) |
| `protocol.py` | JSON-RPC/WebSocket типы сообщений |
| `__init__.py` | Регистрация плагина (`register(ctx)`) |

## Лицензия

MIT
