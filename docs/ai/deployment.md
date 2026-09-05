# Публикация и deployment service2

## Что подтверждено репозиторием

`update.sh` описывает существующий способ обновления: рабочая копия Git на
хостинге, virtualenv уровнем выше, Django migrations/collectstatic и перезапуск
через `../tmp/restart.txt`. Это соответствует Passenger-hosting. 5 сентября
владелец подтвердил SSH-сессию Beget, production HEAD `1cc6e08`, Django `5.1.6`
и наличие `../venv`, `../tmp` выводом read-only команд. Собственная SSH-сессия
агента и успешный production deployment этим документом не утверждаются.

## Автоматический цикл

Workflow `.github/workflows/ci-deploy.yml` запускает все Django-тесты и проверки
на pull request в `main`. После merge GitHub повторяет те же проверки для
точного commit в `main` и только после успеха запускает deployment в GitHub
Environment `production`. Deployment по SSH вызывает `update.sh` с SHA
проверенного commit. Workflow дополнительно проверяет через GitHub API, что SHA
получен merge одного PR и актуальный head этого PR независимо одобрен не автором.
Проверяются все страницы reviews, а для каждого проверяющего учитывается последнее
решающее состояние (`APPROVED`, `CHANGES_REQUESTED` или `DISMISSED`);
прямой push закрывается fail-closed. Проверенный `update.sh` передаётся на stdin
SSH, поэтому запуск не зависит от старой копии скрипта на сервере. Скрипт сверяет
SHA с `origin/main`, запрещает параллельный
запуск и отказывается затирать настоящие местные изменения. Проверяемое
исключение для результатов сборки статики описано ниже.

Отдельный AI Review выполняется приложением по фактическому diff PR и конкретному
head SHA. После решения `accepted` poller публикует GitHub `APPROVED` от
отдельной service-account identity и включает GitHub auto-merge. Это не текст
ACCEPT разработчика: доставка разрешена только для сохранённой итерации
`ai_review`, связанной с тем же PR head. При новом commit приложение повторно
читает diff и проводит новую проверку. `development-codex.yml` остаётся точкой
реализации и публикации PR; задача попадает туда из карточки DevelopmentTask
через серверный dispatch, а `poll_development_codex` ведёт
review/correction/delivery без пересылки между чатами.

Независимость дополнительно обеспечивает правило защищённой ветки, а не deploy-скрипт.
Для `main` нужно включить protection/ruleset со следующими условиями:

- изменения только через pull request;
- минимум один approval пользователя, который не является автором;
- dismiss stale approvals после новых commits;
- обязательная проверка `Django checks and tests`;
- запрет force push и удаления ветки;
- запрет обхода правил, включая администратора, для обычной публикации.

## Однократная настройка GitHub

PR публикуется штатным `github-actions[bot]`. Поскольку созданное встроенным
`GITHUB_TOKEN` событие PR само не запускает следующий workflow,
`development-codex.yml` явно делает `workflow_dispatch` проверки
`ci-deploy.yml` на опубликованной ветке. Этот запуск выполняет только тесты и
создаёт check для head SHA; merge/deployment при `workflow_dispatch` исключены.

Создать отдельную GitHub service account (не identity, которой создаётся PR),
выдать ей fine-grained token с `Pull requests: write` и доступом к этому
репозиторию, затем задать на сервере:

- `GITHUB_DEVELOPMENT_REVIEW_TOKEN` — token отдельной reviewer identity;
- `GITHUB_DEVELOPMENT_REVIEW_LOGIN` — точный GitHub login этой identity;
- `GITHUB_DEVELOPMENT_AUTO_MERGE_ENABLED=true` — только после настройки ruleset;
- включить GitHub auto-merge в настройках репозитория.

Reviewer identity проверяется через GitHub API и обязана отличаться от автора
PR. Auto-merge только ставится в очередь: объединение выполняет GitHub после
всех required checks и действующего approval актуального head.

Создать Environment `production` и repository/environment secrets:

- `DEPLOY_HOST` — SSH hostname;
- `DEPLOY_PORT` — SSH port (можно оставить пустым для 22);
- `DEPLOY_USER` — отдельный deployment user;
- `DEPLOY_APP_PATH` — абсолютный путь к checkout, содержащему `update.sh`;
- `DEPLOY_SSH_KEY` — закрытый ключ ограниченного deployment user;
- `DEPLOY_KNOWN_HOSTS` — заранее проверенная строка host key, не результат
  отключения `StrictHostKeyChecking`.
- `DEPLOY_HEALTH_URL` — обязательный HTTPS URL без userinfo, query и fragment для
  ограниченной проверки после перезапуска; успешным считается только HTTP 2xx.

Публичный ключ должен иметь только права, необходимые для обновления этого
приложения. Production checkout должен иметь read-доступ к GitHub `origin` и
существующие `.env`, `../venv` и `../tmp`. Секреты не записываются в Git.

После первой настройки workflow нужно запустить на тестовом PR, независимо
проверить его, merge выполнить через защищённый `main`, затем проверить GitHub
deployment log и фактический health приложения. До этой проверки схема считается
подготовленной, но не доказанной на хостинге.

Перед изменяющим SSH-шагом workflow запускает `update.sh` в режиме `preflight`.
Он использует read-only `git ls-remote`, проверяет точный `origin/main`,
Passenger-layout, virtualenv, writable `tmp` и допустимость состояния checkout.
Он не выполняет fetch/checkout/pip/migrate/collectstatic/restart. Найденные
неизвестные локальные изменения не выводятся и не удаляются: deployment
останавливается.

## Собранная статика в существующем checkout

`STATIC_ROOT` — `public_static`, а исходники приложения находятся в
`pool_service/static`. В репозитории исторически отслеживается часть собранных
файлов: после `collectstatic` Git может показывать обычные результаты сборки как
местные изменения. В сессии владельца все 21 показанный файл совпали с
исходниками. Это не основание игнорировать весь `public_static`.

Preflight допускает изменённый или untracked файл в `public_static` только если
это обычный файл без symlink-компонентов, а соответствующий исходник в
`pool_service/static` совпадает и с ним, и с обычным Git blob текущего HEAD.
Staged changes, удаления, неизвестные файлы и несовпадения блокируют deployment.
`backups/` не получает исключения; её содержимое сохраняется на сервере и
требует отдельной сверки, без отправки в репозиторий.

В изменяющем режиме после fetch проверка повторяется с доступным точным target
commit. Коллизии untracked output с отслеживаемым target path, удаление или
смена типа соответствующих исходников/отслеживаемых output блокируют обновление
до подготовки файлов. Успех preflight без локального target commit не доказывает
отсутствия таких коллизий: они обязательно проверяются после fetch.

Перед checkout только проверенные изменённые tracked output копируются в
отдельный приватный каталог `../tmp/service2-static-*`, и копия сверяется.
Затем только эти пути восстанавливаются к HEAD для переключения Git. Сохранённые
байты возвращаются после checkout, включая его ошибку. Если тип пути или symlink
мешает безопасному восстановлению, deployment останавливается, а копия
сохраняется для восстановления. Untracked output не удаляются. Резервная копия
остаётся вне checkout. Это копия статики, не backup БД
и не обещание атомарного deployment или rollback миграций.

Не применять `git reset --hard`, `git clean` или широкие исключения
`public_static/` и `backups/` для обхода этой проверки.

`ADVISOR_MCP_TEST_ENABLED` workflow устанавливает в `false` только в CI.
Deployment не меняет production `.env`; тестовый MCP нельзя включать в рамках
этой настройки.

`workflow_dispatch` выполняет только тесты: review gate и deployment для него
пропускаются.

Deployment не является атомарным и автоматический rollback после применённой
миграции не обещается. Ошибка останавливает последующие шаги, но уже применённая
миграция или установленная зависимость может остаться. Для рискованных миграций
до merge обязателен проверенный backup рабочей БД и отдельный план roll-forward;
возврат к старому коду допустим только после проверки совместимости схемы.
