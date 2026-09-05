# Публикация и deployment service2

## Что подтверждено репозиторием

`update.sh` описывает существующий способ обновления: рабочая копия Git на
хостинге, virtualenv уровнем выше, Django migrations/collectstatic и перезапуск
через `../tmp/restart.txt`. Это соответствует Passenger-hosting. Наличие SSH,
точный провайдер, адрес, пользователь и путь из доступной рабочей копии не
установлены; успешный production deployment этим документом не утверждается.

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
запуск и отказывается затирать изменённые tracked-файлы.

Независимое ревью обеспечивает правило защищённой ветки, а не deploy-скрипт.
Для `main` нужно включить protection/ruleset со следующими условиями:

- изменения только через pull request;
- минимум один approval пользователя, который не является автором;
- dismiss stale approvals после новых commits;
- обязательная проверка `Django checks and tests`;
- запрет force push и удаления ветки;
- запрет обхода правил, включая администратора, для обычной публикации.

## Однократная настройка GitHub

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
