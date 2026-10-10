#!/usr/bin/env bash
# autopara — встановлення одним рухом (Linux, macOS).
# Запусти в теці програми: ./install.sh   (або: bash install.sh)
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

python=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
if [ -z "$python" ]; then
  echo "Потрібен Python 3.11 або новіший:"
  echo "  Arch:   sudo pacman -S python"
  echo "  Ubuntu: sudo apt install python3 python3-venv"
  echo "  macOS:  brew install python"
  exit 1
fi

say "1/6 Окреме середовище Python (.venv)"
[ -d .venv ] || "$python" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip

say "2/6 autopara з розпізнаванням мови"
# З відеокартою NVIDIA — ще й бібліотеки CUDA (~1,5 ГБ): розпізнавання на ній у рази швидше (див. autopara/accel.py).
extras=$(.venv/bin/python -c "import sys; sys.path.insert(0, '.'); from autopara.accel import pip_extras; print(pip_extras())" 2>/dev/null || echo ears)
[ "$extras" = "ears" ] || echo "Бачу відеокарту NVIDIA — розпізнаватиму на ній (докачаю бібліотеки CUDA, ~1,5 ГБ)."
if ! .venv/bin/python -m pip install -q -e ".[$extras]" && ! .venv/bin/python -m pip install -q -e ".[ears]"; then
  echo "Розпізнавання мови не встановилось — ставлю без нього (доставити можна кнопкою у вікні програми)."
  .venv/bin/python -m pip install -q -e .
fi

say "3/6 Браузер програми (у ньому вона заходить на пари; ~150 МБ)"
.venv/bin/python -m playwright install chromium --no-shell \
  || echo "Не вийшло — не страшно: програма завантажить браузер сама при першому запуску."

say "4/6 Мозок програми — Ollama (сповіщення про оголошення, звіти після пар, відповіді бота)"
# Без Ollama працюють вхід на пари, вуха й команди; мовні моделі для неї завантажує крок 6.
if command -v ollama >/dev/null 2>&1; then
  echo "Ollama вже є."
elif [ ! -t 0 ]; then
  echo "Встанови Ollama пізніше: https://ollama.com/download — мозок програма підключить сама."
else
  if [ "$(uname)" = "Darwin" ]; then
    how="brew install ollama"
    command -v brew >/dev/null 2>&1 || how=""
  elif command -v pacman >/dev/null 2>&1; then
    how="sudo pacman -S --needed ollama && sudo systemctl enable --now ollama"
  else
    how="curl -fsSL https://ollama.com/install.sh | sh"
  fi
  if [ -z "$how" ]; then
    echo "Встанови Ollama з https://ollama.com/download (перетягни в «Програми» й запусти) — мозок програма підключить сама."
  else
    printf 'Встановити Ollama зараз? Виконаю: %s\n(може спитати пароль від комп'"'"'ютера) [Т/н] ' "$how"
    read -r answer
    case "$answer" in
      [нНnN]*) echo "Пропускаю. Поставити можна будь-коли: https://ollama.com/download" ;;
      *) sh -c "$how" || echo "Не вийшло — встанови пізніше з https://ollama.com/download, мозок підключиться сам." ;;
    esac
    if [ "$(uname)" = "Darwin" ] && command -v ollama >/dev/null 2>&1; then
      brew services start ollama >/dev/null 2>&1 || true
    fi
  fi
fi

say "5/6 Ярлик"
app="$PWD/.venv/bin/autopara"
if [ "$(uname)" = "Darwin" ]; then
  mkdir -p "$HOME/Applications"
  printf '#!/bin/bash\n"%s" app\n' "$app" > "$HOME/Applications/autopara.command"
  chmod +x "$HOME/Applications/autopara.command"
  echo "Ярлик: ~/Applications/autopara.command"
else
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/autopara.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=autopara
Comment=Автоматичний вхід на пари
Exec="$app" app
Icon=$PWD/autopara/gui/icon.svg
Terminal=false
Categories=Education;
StartupWMClass=autopara
EOF
  echo "Ярлик «autopara» тепер є в меню програм."
  if [ "${XDG_SESSION_TYPE:-}" = "x11" ] && command -v ldconfig >/dev/null 2>&1 && ! ldconfig -p | grep -q "libxcb-cursor.so.0"; then
    echo "Для вікна програми потрібна бібліотека libxcb-cursor (Ubuntu/Mint: sudo apt install libxcb-cursor0)."
  fi
  if ! command -v pactl >/dev/null 2>&1; then
    echo "Порада: щоб вуха чули Zoom-клієнт, встанови pactl (Arch: sudo pacman -S libpulse; Ubuntu: sudo apt install pulseaudio-utils)."
  fi
fi

say "6/6 Моделі розпізнавання мови й мозку (кілька ГБ — один раз, щоб перша пара не чекала)"
if [ "${AUTOPARA_NO_MODELS:-}" = 1 ]; then
  echo "Пропускаю (AUTOPARA_NO_MODELS=1) — програма докачає сама."
else
  .venv/bin/autopara models || echo "Не все вийшло — програма докачає сама, коли знадобиться."
fi

say "Готово! Відкриваю програму — далі простий майстер."
if [ "${AUTOPARA_NO_LAUNCH:-}" != 1 ]; then
  nohup "$app" app >/dev/null 2>&1 &
  disown || true
fi
