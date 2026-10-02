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

say "1/4 Окреме середовище Python (.venv)"
[ -d .venv ] || "$python" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip

say "2/4 autopara з розпізнаванням мови"
if ! .venv/bin/python -m pip install -q -e ".[ears]"; then
  echo "Розпізнавання мови не встановилось — ставлю без нього (доставити можна кнопкою у вікні програми)."
  .venv/bin/python -m pip install -q -e .
fi

say "3/4 Запасний браузер (знадобиться, лише якщо немає Google Chrome)"
.venv/bin/python -m playwright install chromium || echo "Не вийшло — не страшно, якщо встановлений Google Chrome."

say "4/4 Ярлик"
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
Icon=x-office-calendar
Terminal=false
Categories=Education;
EOF
  echo "Ярлик «autopara» тепер є в меню програм."
  if ! command -v pactl >/dev/null 2>&1; then
    echo "Порада: щоб вуха чули Zoom-клієнт, встанови pactl (Arch: sudo pacman -S libpulse; Ubuntu: sudo apt install pulseaudio-utils)."
  fi
fi

say "Готово! Відкриваю вікно програми — далі все там."
if [ "${AUTOPARA_NO_LAUNCH:-}" != 1 ]; then
  nohup "$app" app >/dev/null 2>&1 &
  disown || true
fi
