# Быстрый старт Continuum

[English](quickstart.md) | [Русский](quickstart.ru.md)

Эта инструкция проводит **новый изолированный тестовый репозиторий** от нуля
до одной автономной реализации задачи. Она намеренно начинается с негативной
проверки: обычная задача не должна запускаться, пока вы явно не допустите её.

> [!WARNING]
> **Не запускайте это в существующем репозитории потребителя.** Используйте
> одноразовый репозиторий без ценных задач. Шаги создания, использования и
> запуска Actions в этом документе **не выполнялись вживую** для текущего
> поколения репозитория; см.
> [Статус живой проверки](#статус-живой-проверки).

## Что вы проверите

1. Установщик запишет core-вызыватели и не тронет ваш проектный `ci.yml`.
2. Обычная задача не запускается, пока задан `AUTOMATION_READY_LABEL`.
3. Безобидная задача с меткой допуска запускается, реализуется в ветке,
   открывается как pull request, проверяется CI и сливается только при
   прохождении гейтов.

## Перед началом

Вам понадобятся:

- аккаунт GitHub и репозиторий, где у вас есть права администратора;
- включённые GitHub Actions;
- ветка, которую нужно защитить, обычно `main`;
- **классический** PAT со scopes `repo` + `workflow`, сохранённый как `TAP_PAT`;
- при желании — приложение CodeRabbit или конфигурация PR-Agent, если нужен
  провайдер ревью.

Ядро использует бесплатный анонимный маршрут модели OpenCode. Платный ключ
провайдера не нужен. Бесплатные маршруты могут быть ограничены по частоте или
недоступны; инструкция не обещает, что каждая реализация или ревью завершатся
успешно.

## 1. Создайте изолированного тестового потребителя

Из клона Continuum задайте `CONTINUUM_DIR` с его путём. Затем создайте тестовый
репозиторий и скопируйте только файлы примера проекта:

```sh
export CONTINUUM_DIR=/path/to/continuum
export TEST_REPO="YOUR_GITHUB_OWNER/continuum-quickstart-test"

gh repo create "$TEST_REPO" --private --clone
cd continuum-quickstart-test

cp "$CONTINUUM_DIR/examples/minimal-consumer/app.py" .
cp -R "$CONTINUUM_DIR/examples/minimal-consumer/tests" .
mkdir -p .github/workflows
cp "$CONTINUUM_DIR/examples/minimal-consumer/.github/workflows/ci.yml" \
  .github/workflows/ci.yml
```

Пример проекта намеренно крошечный: один Python-модуль и один тест на
стандартной библиотеке. Его `ci.yml` принадлежит проекту и вызывает
переиспользуемый движок валидации Continuum.

## 2. Изучите и закрепите Continuum

Выберите одну политику ссылок для первого запуска:

- **Воспроизводимая (рекомендуется):** используйте один и тот же полный SHA
  коммита для установщика, установленных вызывателей и проектного `ci.yml`.
  Ссылка `uses:` вызывателя и `continuum_ref:` должны совпадать.
- **Намеренно подвижная:** оставьте и `uses:` вызывателя, и `continuum_ref:` на
  `main`, и устанавливайте тоже с `main`. Это допустимо, только если вы выбрали
  это для обеих ссылок и принимаете, что workflow и его движок двигаются
  вместе. Никогда не смешивайте закреплённый вызыватель с незакреплённым
  `continuum_ref:` или наоборот.

В отличие от установленных вызывателей, `continuum-validation.yml` берёт
ревизию только из `uses:` вызывателя; значение `continuum_ref:` в проектном
`ci.yml` не влияет на ревизию движка. Установленные вызыватели (например,
`continuum-opencode.yml`) действительно читают `continuum_ref:`, чтобы выбрать
свой checkout движка, поэтому правило совпадения относится к ним. Приведённая
ниже правка всё равно обновляет обе строки, чтобы пример оставался
согласованным.

Шаги ниже реализуют воспроизводимый путь. Пример `ci.yml` поставляется с
подвижными ссылками на `main`; правка ниже меняет ссылку `uses:` (и неактивный
`continuum_ref:`) на выбранный SHA. Для намеренно подвижного варианта
**замените** присваивание `export CONTINUUM_REF=...` в следующем блоке на
`export CONTINUUM_REF=main` **до** скачивания установщика, чтобы скачанный
скрипт соответствовал выбранной ссылке. Не запускайте закреплённое присваивание,
а затем не пытайтесь переопределить его сверху.

```sh
export CONTINUUM_REF="$(git -C "$CONTINUUM_DIR" rev-parse HEAD)"

curl -fsSL \
  "https://raw.githubusercontent.com/kodmial/continuum/${CONTINUUM_REF}/install.sh" \
  -o ../continuum-install.sh
less ../continuum-install.sh
sha256sum ../continuum-install.sh
```

Закрепите проектный CI на той же ревизии. Установщик переписывает вызыватели,
но намеренно никогда не правит ваш `ci.yml`.

```sh
python3 - "$CONTINUUM_REF" <<'PY'
from pathlib import Path
import re
import sys

ref = sys.argv[1]
path = Path(".github/workflows/ci.yml")
text = path.read_text(encoding="utf-8")

text, uses_count = re.subn(
    r"(continuum-validation\.yml@)[^\s\"']+",
    lambda match: match.group(1) + ref,
    text,
)
text, ref_count = re.subn(
    r"(?m)^(\s*continuum_ref:\s*)[^\s#]+",
    lambda match: match.group(1) + ref,
    text,
)

if uses_count != 1 or ref_count != 1:
    raise SystemExit(
        "expected exactly one continuum-validation.yml ref and one "
        f"continuum_ref in {path}; found {uses_count} and {ref_count}"
    )

path.write_text(text, encoding="utf-8")
PY
```

Скрипт завершается ошибкой, а не пропускает замену молча, если пример `ci.yml`
разошёлся, поэтому CI нельзя оставить незакреплённым из-за несовпавшей
константы.

Для намеренно подвижного варианта пропустите правку `ci.yml` и установите с
той же ссылкой. Если `../continuum-install.sh` уже скачан из закреплённого
checkout, скачайте его заново после `CONTINUUM_REF=main`, чтобы установщик
соответствовал выбранной ссылке; в примере из репозитория уже стоят
`uses: ...@main` и `continuum_ref: main`.

Как альтернативу используйте локальный установщик **только для пути с полным
SHA**, когда checkout Continuum находится ровно на этом коммите. Держите
проверку и установку в одной условной команде: несовпадение должно не дать
установщику запуститься.

```sh
test "${#CONTINUUM_REF}" -eq 40 &&
  test "$(git -C "$CONTINUUM_DIR" rev-parse HEAD)" = "$CONTINUUM_REF" &&
  bash "$CONTINUUM_DIR/install.sh" . "$CONTINUUM_REF" core
```

**Не** используйте локальный вариант для подвижного пути `main`;
используйте скачанный `../continuum-install.sh` на выбранной ссылке. Один
`CONTINUUM_REF` никогда не обновляет уже скачанный установщик.

## 3. Установите core-вызыватели

```sh
bash ../continuum-install.sh . "$CONTINUUM_REF" core
rm ../continuum-install.sh
ls .github/workflows/continuum-*.yml
```

Ожидаемый результат:

- присутствуют core-вызыватели `continuum-*.yml`;
- ваш собственный `.github/workflows/ci.yml` на месте и не изменён;
- второй workflow `CI` не создан.

Установщик никогда не устанавливает в собственный checkout Continuum, а в новом
репозитории нечего удалять.

## 4. Закоммитьте и отправьте базовую линию

Новый репозиторий на удалённом сервере всё ещё пуст. GitHub Actions планирует
только workflows, существующие в ветке по умолчанию, а защите ветки нужен
существующий `main`, поэтому закоммитьте пример проекта, `ci.yml` и
установленные вызыватели, затем отправьте их до открытия любой задачи:

```sh
git add -A
git commit -m "Add Quick Start baseline and Continuum core callers"
git branch -M main
git push -u origin main
```

Убедитесь, что на удалённом сервере теперь есть `main` и установленные
workflows:

```sh
git ls-remote --heads origin main
gh workflow list --repo "$TEST_REPO"
```

## 5. Задайте допуск и учётные данные до открытия задач

```sh
gh variable set AUTOMATION_READY_LABEL \
  --repo "$TEST_REPO" --body "automation:ready"

gh secret set TAP_PAT --repo "$TEST_REPO"
```

По запросу вставьте классический PAT со scopes `repo` + `workflow`. Не
помещайте токен в отслеживаемый файл.

`AUTOMATION_READY_LABEL` — важнейшая настройка безопасности:

> [!IMPORTANT]
> Задайте `AUTOMATION_READY_LABEL` **до первой задачи**. Пока переменная пуста,
> планировщик может поставить в очередь любую подходящую открытую задачу.

- **задана** как `automation:ready`: автоматически реализуются только задачи с
  этой меткой;
- **пуста** (историческое значение по умолчанию): планировщик может поставить в
  очередь любую подходящую открытую задачу.

Задайте её **до первой задачи**. Если нужна другая метка, задайте её значение;
в примерах используется `automation:ready`.

Необязательный провайдер ревью (только если он у вас установлен):

```sh
# CodeRabbit
gh variable set CONTINUUM_REVIEW_PROVIDER --repo "$TEST_REPO" --body coderabbit

# Или необязательный стек PR-Agent
gh variable set CONTINUUM_REVIEW_PROVIDER --repo "$TEST_REPO" --body pr-agent
```

По умолчанию — `none`. См.
[раздел о PR-Agent в переменных потребителя](consumer-variables.ru.md#необязательный-провайдер-ревью-pr-agent)
для настройки конкретного провайдера. Платный ключ провайдера ядру не нужен.

## 6. Защитите `main`

В настройках репозитория добавьте правило защиты ветки или ruleset для `main`:

- требовать pull request перед слиянием;
- требовать check run `Validate` переиспользуемого workflow; никакой check run
  буквально не называется `CI`. Необязательный commit-status контекст
  `continuum/validation` намеренно подавляется для PR из форков и PR
  Dependabot, потому что такие запуски не могут публиковать commit status с
  read-only токеном. Не требуйте этот необязательный контекст, если только
  доверенный публикатор не покрывает такие PR. Скопируйте точное имя проверки из
  первого запуска pull request перед сохранением правила;
- требовать актуальности ветки перед слиянием;
- блокировать force push и удаление;
- не выдавать обычные разрешения на обход.

Путь слияния Continuum читает свидетельства текущего HEAD, но именно
серверная защита не даёт человеку или неправильно настроенному workflow писать
напрямую в `main`.

## 7. Негативная проверка: задача без допуска не должна запускаться

Откройте обычную задачу без метки допуска:

```sh
ISSUE="$(gh issue create \
  --repo "$TEST_REPO" \
  --title "Quick Start negative check: no automation:ready" \
  --body "This issue is a negative admission check. It must not be dispatched." \
  | awk '{print $NF}')"
echo "$ISSUE"
```

Событие открытия задачи может запустить планировщик. Это ожидаемо. Проверьте,
что планировщик **не** запустил реализацию:

```sh
gh issue view "$ISSUE" --repo "$TEST_REPO" --json labels,comments,state
gh run list --repo "$TEST_REPO" --workflow continuum-issue-scheduler.yml --limit 5
gh run list --repo "$TEST_REPO" --workflow continuum-opencode.yml --limit 5
```

Ожидается:

- планировщик может показать завершённый запуск;
- метки `automation:in-progress` **нет**;
- комментария о запуске нет;
- запуска реализации `OpenCode agent` нет.

Точная причина пропуска видна в журнале запуска планировщика. Текущая ожидаемая
причина — у задачи нет настроенной метки готовности.

Закройте задачу негативной проверки перед продолжением:

```sh
gh issue close "$ISSUE" --repo "$TEST_REPO"
```

То же поведение офлайн покрывает детерминированная фикстура в
[`examples/minimal-consumer/verification/`](../examples/minimal-consumer/verification/).
Запустите её из клона Continuum:

```sh
cd "$CONTINUUM_DIR"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3 -m unittest discover -s examples/minimal-consumer/verification -v
```

## 8. Позитивная проверка: допустите одну безобидную задачу

Создайте метку допуска, если её ещё нет, затем откройте небольшую задачу и явно
добавьте метку:

```sh
gh label create automation:ready \
  --repo "$TEST_REPO" \
  --description "Admits this issue to automatic implementation by the issue scheduler." \
  --color 0E8A16 || true

ISSUE="$(gh issue create \
  --repo "$TEST_REPO" \
  --title "Quick Start: add a friendly farewell" \
  --body "Add a small, tested farewell helper in app.py, for example farewell(name) returning 'Goodbye, <name>!'. Keep the change minimal and keep the existing tests passing." \
  | awk '{print $NF}')"
echo "$ISSUE"

gh issue edit "$ISSUE" --repo "$TEST_REPO" --add-label automation:ready
```

Событие метки будит планировщик. GitHub может задерживать события расписания,
поэтому подождите несколько минут, а не добавляйте метку повторно.

Ожидаемая наблюдаемая последовательность:

1. планировщик резервирует задачу меткой `automation:in-progress`;
2. появляется комментарий о запуске;
3. стартует запуск workflow `OpenCode agent`;
4. запуск создаёт ветку `opencode/issue<N>-...` и открывает PR с `Closes #N`;
5. проектный workflow `CI` выполняется на HEAD этого PR;
6. если настроен провайдер ревью, выполняется его путь; при `none` внешний гейт
   ревью не добавляется;
7. гейт слияния по exact-HEAD сливает PR только при прохождении настроенных
   гейтов;
8. слитый PR закрывает задачу.

Проверьте результат:

```sh
gh issue view "$ISSUE" --repo "$TEST_REPO" --json state,labels,comments
gh pr list --repo "$TEST_REPO" --state all --limit 10
gh run list --repo "$TEST_REPO" --limit 10
```

Если запуск не удался, прочитайте шаг с ошибкой. Частые причины: отсутствующий
или недостаточно широкий `TAP_PAT`, ограниченная по частоте бесплатная модель,
недостаточно подробная задача, защита ветки или падающая проверка `CI`. Не
считайте, что ремонт или ревью всегда завершатся успешно.

## 9. Очистка

Для одноразового тестового репозитория:

> [!CAUTION]
> Эта команда безвозвратно удаляет тестовый репозиторий и всё его содержимое.

```sh
gh repo delete "$TEST_REPO" --yes
```

Команде нужен scope `delete_repo`, которого нет у стандартного OAuth-токена
`gh`; сначала выполните `gh auth refresh -h github.com -s delete_repo` или
удалите репозиторий через веб-интерфейс GitHub.

Как минимум закройте оставшиеся задачи и удалите лишние ветки `opencode/*`.
Не направляйте этот Quick Start на существующий репозиторий потребителя.

## Статус живой проверки

Это поколение репозитория **не** является живым доказательством сквозного
сценария для внешнего потребителя:

- песочный репозиторий не создавался;
- учётные данные не настраивались;
- запуски GitHub Actions не отправлялись;
- репозиторий потребителя не изменялся.

Офлайн-проверки, которые выполнялись: контрактный набор репозитория, наборы
Python/unittest, синтаксис установщика, `actionlint` для workflows и пример
`ci.yml`, а также детерминированная фикстура допуска. Живой Quick Start требует
отдельного одобрения владельцем тестового репозитория, его учётных данных и его
запусков Actions. До этого считайте документ воспроизводимой процедурой, а не
подтверждением успешного живого E2E.

## Файлы, используемые в инструкции

| Файл | Роль |
| --- | --- |
| [`examples/minimal-consumer/app.py`](../examples/minimal-consumer/app.py) | Код примера приложения. |
| [`examples/minimal-consumer/tests/test_app.py`](../examples/minimal-consumer/tests/test_app.py) | Пример теста на стандартной библиотеке. |
| [`examples/minimal-consumer/.github/workflows/ci.yml`](../examples/minimal-consumer/.github/workflows/ci.yml) | Полный проектный workflow `CI`, вызывающий переиспользуемый движок валидации. |
| [`examples/minimal-consumer/verification/test_ready_admission.py`](../examples/minimal-consumer/verification/test_ready_admission.py) | Детерминированная фикстура негативного и позитивного допуска. |
