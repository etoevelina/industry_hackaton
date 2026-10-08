#!/bin/bash
# Живая проверка профиля экзамена: оболочка грузит «LMS» из фикстуры,
# фильтр разрешает только её, всё остальное обязано блокироваться с инцидентом.
#
# Запускается из Terminal.app — разрешение на камеру macOS выдаёт приложению,
# владеющему процессом, а не Python.
#
# Профиль лежит в каталоге проктора, ядро находит его само третьим источником
# в приоритете (--exam-profile > PROCTOR_EXAM_PROFILE > <каталог>/exam-profile.json).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SCRATCH="/private/tmp/claude-501/-Users-evelinapenkova-Downloads-industry-hackaton/6adbdbd6-fa0b-41f2-b563-e4e934c2ae9f/scratchpad"
EXAMDIR="$SCRATCH/examdir"

if [ ! -f "$EXAMDIR/exam-profile.json" ]; then
    echo "Нет файла профиля: $EXAMDIR/exam-profile.json"
    echo "Нажмите Enter, чтобы закрыть."; read -r _; exit 1
fi

if ! curl -s -o /dev/null --max-time 3 http://127.0.0.1:8850/index.html; then
    echo "Фикстура LMS на 127.0.0.1:8850 не отвечает."
    echo "Поднимите её: cd $SCRATCH/lms && python3 -m http.server 8850 --bind 127.0.0.1 &"
    echo "Нажмите Enter, чтобы закрыть."; read -r _; exit 1
fi

echo "=================================================="
echo " ПРОВЕРКА ПРОФИЛЯ ЭКЗАМЕНА"
echo " Разрешено: только 127.0.0.1:8850"
echo " Блокировки окружения ВЫКЛЮЧЕНЫ (--no-lockdown)"
echo "=================================================="
echo
echo "Что проверяем на экране:"
echo "  1. На согласии виден блок «Правила этого экзамена»"
echo "  2. Открылась страница теста, а не наш мок"
echo "  3. Ссылки Google / Википедия / ChatGPT / file:// НЕ открываются"
echo "  4. Каждая попытка — инцидент в ленте HUD с адресом"
echo "  5. Внешняя картинка на странице не загрузилась (подзапрос отрезан)"
echo

# Камера задаётся явно: на этой машине встроенная FaceTime — индекс 1,
# индекс 0 занимает iPhone как Continuity Camera.
exec env PROCTOR_SESSIONS_DIR="$EXAMDIR" PROCTOR_CAMERA="${PROCTOR_CAMERA:-1}" \
    "$(dirname "$0")/run-safe.command"
