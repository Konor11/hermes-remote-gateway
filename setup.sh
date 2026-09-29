#!/usr/bin/env bash
# One-shot setup: from nothing to a working remote connection.
#
#   bash setup.sh                                  # спросит URL и способ входа
#   bash setup.sh --url https://my.domen.com      # без вопросов (auth спросит)
#   bash setup.sh --url https://… --auth basic --user login --pass '…'
#   bash setup.sh --local-pc --server-password '…'   # ещё и файлы ноута
#   bash setup.sh --check                         # только показать, что уже есть
#
# Каждый шаг идемпотентен: скрипт можно запускать повторно, он ничего не ломает
# и не переустанавливает готовое.
set -uo pipefail

REPO="${REPO:-https://github.com/Konor11/hermes-remote-gateway.git}"
PLUGIN_DIR="${HERMES_HOME:-$HOME/.hermes}/plugins/hermes-remote-gateway"
HERMES="${HERMES:-hermes}"

# ---- аргументы ----------------------------------------------------------
URL=""; AUTH=""; USER=""; PASS=""; TOKEN=""; PROFILE=""
LOCAL_PC=0; SERVER_PASSWORD=""; CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --auth) AUTH="$2"; shift 2 ;;
    --user) USER="$2"; shift 2 ;;
    --pass) PASS="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --local-pc) LOCAL_PC=1; shift ;;
    --server-password) SERVER_PASSWORD="$2"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "неизвестный аргумент: $1 (см. --help)" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }
die()  { printf '  ✗ %s\n' "$*" >&2; exit 1; }

ask() { # ask <переменная> <подсказка> <default>
  local __var="$1" prompt="$2" default="${3:-}" ans=""
  if [ -n "${!__var}" ]; then eval "$__var=\"\${!__var}\""; return 0; fi
  if [ ! -t 0 ]; then
    eval "$__var=\"\$default\""
    return 0
  fi
  printf '  %s' "$prompt"
  [ -n "$default" ] && printf ' [%s]' "$default"
  read -r ans </dev/tty
  [ -z "$ans" ] && ans="$default"
  eval "$__var=\"\$ans\""
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- что уже есть -------------------------------------------------------
if [ "$CHECK" = 1 ]; then
  step "Проверка (ничего не меняется)"
  have "$HERMES" && say "  hermes:            ✅ $(command -v "$HERMES")" || say "  hermes:            ❌ не найден"
  [ -d "$PLUGIN_DIR" ] && say "  плагин:            ✅ $PLUGIN_DIR" || say "  плагин:            ❌ не установлен"
  if have "$HERMES"; then
    "$HERMES" remote --help >/dev/null 2>&1 && say "  команда remote:    ✅" || say "  команда remote:    ❌ плагин не включён"
    "$HERMES" remote status 2>/dev/null | sed 's/^/    /' || true
  fi
  if [ -x "${PLUGIN_DIR}/deps.py" ] || [ -f "${PLUGIN_DIR}/deps.py" ]; then
    PY="$(command -v python3 || command -v python)"
    [ -n "$PY" ] && (cd "$PLUGIN_DIR" && "$PY" deps.py) | sed 's/^/  /'
  fi
  exit 0
fi

# ---- 1. плагин ----------------------------------------------------------
step "1/4  Плагин"
if [ -d "$PLUGIN_DIR/.git" ]; then
  say "  обновляю: git pull"
  git -C "$PLUGIN_DIR" pull --ff-only -q 2>/dev/null || warn "не смог обновить (локальные изменения?) — продолжаю с текущей версией"
elif [ -f "$PLUGIN_DIR/plugin.yaml" ]; then
  # Уже установлен, но без git (например, распакован вручную или скопирован).
  say "  плагин уже на месте, но без .git — обновлю только если сам склонируешь заново"
  say "  (подсказка: rm -rf '$PLUGIN_DIR' && git clone $REPO '$PLUGIN_DIR')"
else
  say "  клонирую в $PLUGIN_DIR"
  mkdir -p "$(dirname "$PLUGIN_DIR")"
  git clone -q --depth 1 "$REPO" "$PLUGIN_DIR" || die "не удалось склонировать $REPO"
fi

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || die "нужен python3"

# ---- 2. включаем --------------------------------------------------------
step "2/4  Включаю плагин"
if [ -f "${PLUGIN_DIR}/install.sh" ] && [ -d "${PLUGIN_DIR}/.git" ]; then
  say "  ставлю системные зависимости (sshd, sshpass) — если нужно"
  bash "${PLUGIN_DIR}/install.sh" 2>&1 | sed 's/^/  /' | head -20
fi
have "$HERMES" || die "команда hermes не найдена — поставь Hermes и повтори"
"$HERMES" plugins enable hermes-remote-gateway >/dev/null 2>&1 && say "  ✅ hermes-remote-gateway включён" || warn "не смог включить плагин автоматически (возможно, уже включён)"
"$HERMES" remote --help >/dev/null 2>&1 || die "команда 'hermes remote' не появилась — проверь 'hermes plugins enable hermes-remote-gateway'"

# ---- 3. адрес и вход ----------------------------------------------------
step "3/4  Адрес сервера и вход"
if [ -z "$URL" ]; then
  say "  URL берётся из remote_gateway.url — это адрес твоего Hermes gateway (тот же,"
  say "  что в веб-интерфейсе: https://<домен>)"
fi
ask URL "URL gateway: " "https://mydomen.com"
[ -n "$URL" ] || die "нужен URL"
"$HERMES" config set remote_gateway.url "$URL" >/dev/null || die "не записался remote_gateway.url"

ask AUTH "Как входим? oauth | basic | token: " "oauth"
case "$AUTH" in
  basic)
    ask USER "логин: " "login"
    ask PASS "пароль: " ""
    [ -n "$USER" ] && [ -n "$PASS" ] || die "для basic нужны логин и пароль"
    "$HERMES" config set remote_gateway.username "$USER" >/dev/null
    "$HERMES" config set remote_gateway.password "$PASS" >/dev/null
    ;;
  token)
    ask TOKEN "session token из дашборда: " ""
    [ -n "$TOKEN" ] || die "для token нужен session token"
    "$HERMES" config set remote_gateway.token "$TOKEN" >/dev/null
    ;;
  oauth) ;;
  *) die "неизвестный способ: $AUTH (нужен oauth | basic | token)" ;;
esac
"$HERMES" config set remote_gateway.auth "$AUTH" >/dev/null
[ -n "$PROFILE" ] && "$HERMES" config set remote_gateway.profile "$PROFILE" >/dev/null
say "  ✅ url=$URL  auth=$AUTH"

# ---- 4. подключение -----------------------------------------------------
step "4/4  Подключаюсь"
if [ "$LOCAL_PC" = 1 ]; then
  say "  включаю доступ к файлам этого ноута"
  "$HERMES" config set remote_gateway.local_pc_access true >/dev/null
  if [ -n "$SERVER_PASSWORD" ]; then
    "$HERMES" remote pc setup --server-password "$SERVER_PASSWORD" 2>&1 | sed 's/^/  /'
  else
    "$HERMES" remote pc setup 2>&1 | sed 's/^/  /'
  fi
fi

CONN_OUT="$("$HERMES" remote connect 2>&1)"
CONN_RC=$?
printf '%s\n' "$CONN_OUT" | sed 's/^/  /'

if [ $CONN_RC -ne 0 ] || printf '%s' "$CONN_OUT" | grep -qiE "error|failed|❌"; then
  # Не выдаём «готово» за провалившееся подключение — сначала разберёмся.
  printf '\n  %s\n' "✗ подключиться не вышло. Настройки сохранены (шаг 3), правь и повтори:"
  say "      hermes config set remote_gateway.url '$URL'"
  [ "$AUTH" = "basic" ] && say "      hermes config set remote_gateway.username '$USER'"
  [ "$AUTH" = "token" ] && say "      hermes config set remote_gateway.token '<session token>'"
  say "      hermes remote connect"
  say ""
  say "  Проверить, что уже есть: bash setup.sh --check"
  exit 1
fi

step "Готово"
say "  Теперь просто:"
say "      hermes"
say ""
say "  Вернуть локальный Hermes:  hermes remote disconnect"
say "  Проверить состояние:       hermes remote status"
