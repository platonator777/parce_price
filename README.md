# Обработка тарифов из HTML и API

## Требования

- Для генерации нового адаптера: установленный Ollama и модель qwen3:8b.
  Применение готовых адаптеров не требует Ollama. Веса модели не входят в архив.


## 1. Установка

Склонируйте репозиторий, откройте PowerShell и перейдите в его корень,
где находятся pyproject.toml и run_pipeline.py.

```powershell
py -3.11 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-build.txt -r requirements.txt
.venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps .
```


## 2. Запуск готового адаптера — без нейросети

### Смешанный файл после сбора через API (МТС и Билайн) и HTML

Из корня проекта запускайте общий файл без `--provider`:

```powershell
.venv/Scripts/python.exe run_pipeline.py --input combined_results_20261005_151843.parquet
```

Входной Parquet не включён в репозиторий: положите его в корень или укажите полный путь.
Поддерживается столбец `content`: JSON/HTML определяется отдельно для каждой строки.
Адаптер выбирается по `provider`; `wifire_ru` использует `wifireru`,
`ooofirmaintersvyaz` — `intersvyaz`. Для остальных HTML-сайтов применяется
универсальный адаптер `ones`, с сохранением исходного имени оператора в CSV.
Этот режим не вызывает Ollama. Результаты: `output-full/combined/offers.csv`,
`errors.jsonl` и `quality_report.json` со статистикой по операторам.
Для одного оператора из общего файла добавьте `--provider mts` или
`--provider beeline`: если столбец `provider` существует, чужие строки
пропускаются. `--provider ones` сохраняет прежний режим универсального адаптера.

Записи со статусом `error` и пустым содержимым попадают в `errors.jsonl`;
остальные строки обрабатываются до конца. Ненулевой код завершения в таком
случае означает наличие ошибок во входных данных, готовый CSV сохраняется.
`records_empty` и `providers.*.empty` показывают успешно разобранные страницы
без найденных тарифов; это не подтверждение отсутствия предложений у оператора.

МТС: цена берётся из `totalPrice.value`, старая цена — из `oldValue`,
скорость — из `internetTariff.speed` (Гбит/с переводятся в Мбит/с), ТВ —
из `tvPackage.channelsCount`, мобильные пакеты — из `productFeatureGroups`.
`subscriptionFee` и `discountFee` сохраняются в `billing_variants`.
Неуказанный период оплаты остаётся пустым. Билайн: извлекаются только
`blocks[alias=catalog].data.tariffs`, используются `price.fee`/`oldFee`,
`parameters` и `mobileParams`. Нулевая `oldFee` означает отсутствие старой цены.
Безлимит хранится в `conditions`, а не как конечный пакет ГБ.
Подарочная SIM с отдельной платой и Яндекс Плюс отмечаются как дополнительные
услуги. `raw_offer` сохраняет исходный объект тарифа со всеми подробностями.

Если у МТС есть `internetOptions`, каждая позиция ползунка становится отдельной
строкой CSV: 2/3/4 позиции дают 2/3/4 строки. Цена, скорость и платежи берутся
из выбранной позиции; исходная карточка не добавляется как лишний вариант.
`variant_id` различает позиции, даже если их цены и скорости совпадают.
`raw_offer` такого варианта содержит родительскую карточку и выбранную опцию.
Валидация API проверяет полноту списка, лишние строки и дубли вариантов.

Для Wifire.ru извлекается `initialTariffs` из сохранённых Next.js-данных:
отдельно учитываются скорости и тип жилья, включая нулевые промоцены.
Дополнительное ТВ в пакетах не выдаётся за включённое.

`run_pipeline.py` использует `src` из своей папки, поэтому установленная ранее
копия пакета не подменяет исправления. Для команды `price-monitor` пакет нужно
переустановить, как описано ниже.

В проекте есть небольшой реальный пример Parquet с десятью страницами разных операторов:

```powershell
.venv/Scripts/python.exe run_pipeline.py --provider ones --input examples/ones.parquet
```

Для собственного источника MTS:

```powershell
.venv/Scripts/python.exe run_pipeline.py --provider mts --input data/my_mts.csv
```

По умолчанию существующий adapters/mts/adapter.py применяется без перегенерации.
Готовые адаптеры: mts, beeline, rtk, letai, intersvyaz, profintel, sibset,
ttk, wifire, wifireru, ones. Ones — смешанный набор, оператор виден в source_url.

Для всех своих файлов укажите пути в providers.json и выполните:

```powershell
.venv/Scripts/python.exe run_pipeline.py --all
```

Все файлы конфигурации должны существовать: перед началом выполняется проверка путей.
Удалите из конфигурации провайдеров, которых запускать не нужно.
Относительные пути run_pipeline.py разрешает относительно корня проекта,
абсолютные пути тоже поддерживаются. Исходные файлы открываются только для чтения.

## 3. Новый провайдер: генерация → проверка → полный CSV

Установите/запустите Ollama, скачайте модель:

```powershell
ollama pull qwen3:8b
```

Сервер должен быть доступен по http://127.0.0.1:11434. При необходимости
запустите ollama serve в отдельном терминале. Затем:

```powershell
.venv/Scripts/python.exe run_pipeline.py --provider new --input data/new.csv
```

Если адаптера ещё нет, скрипт автоматически генерирует его, проверяет train/holdout
и блокирует нормализацию при ошибках или отсутствии smoke. Затем обрабатывает весь исходный файл.
Нужен достаточно большой источник для формирования отдельных выборок.
По умолчанию — максимум пять попыток генерации/исправления.

Подсказки генерации и исправления ограничены безопасным UTF-8 бюджетом
с резервом для ответа: инструкции не обрезаются переполнением контекста.
Для JSON используется сокращённая подсказка; ошибки повторной попытки
относятся именно к показанному модели коду и включают причину падения.
Примеры JSON отправляются целыми, без путаницы схемы с реальными массивами.
При генерации также проверяется, что явные цены, скорость SHPD и известные
коды включённых услуг не потерялись в извлечённых полях.
Обновление файлов адаптера и состояния допускает короткую временную блокировку
Windows: атомарная замена повторяется до 20 раз с паузой 50 мс.
Это не гарантирует успешную генерацию для любого нового источника:
проверки по-прежнему блокируют ошибочный адаптер.

При обновлении уже установленного проекта переустановите пакет, иначе Python
продолжит использовать старую копию из окружения:

```powershell
.venv/Scripts/python.exe -m pip install --no-build-isolation --no-deps --force-reinstall .
```

Для принудительной перегенерации существующего адаптера:

```powershell
.venv/Scripts/python.exe run_pipeline.py --provider new --input data/new.csv --regenerate
```

Перегенерация заменяет адаптер; если хотите сохранить предыдущий, передайте
другую директорию --adapters adapters-new. Другие параметры: --model,
--ollama-url, --max-attempts, --adapter-timeout, --output.

Пример полного цикла генерации на искусственных демонстрационных данных:

```powershell
.venv/Scripts/python.exe run_pipeline.py --provider demo --input examples/demo.csv
```

## Результаты

```text
output-full/<provider>/
  offers.csv          нормализованные тарифы, UTF-8 BOM
  errors.jsonl        ошибки отдельных записей
  quality_report.json статистика выполнения
  run_state.json      состояние штатного runner
adapters/<provider>/
  adapter.py
  manifest.json и отчёты генерации — для вновь созданных адаптеров
```

CSV содержит provider, city, region, source_url, timestamp, availability, name, variant_id,
price, price_old, price_period, connection_cost, technology, internet_speed_mbps,
tv_channels, mobile_minutes, mobile_data_gb, services, source_record_id,
evidence, raw_offer, conditions, optional_services, billing_variants.
Списки и raw_offer записаны JSON внутри ячейки. Цены в рублях, скорость в Мбит/с.
Явная годовая цена имеет период «год»; неуказанный период остаётся null.

Обычный повторный запуск перезаписывает результаты данного провайдера.
Для продолжения прерванного штатного runner используйте:

```powershell
.venv/Scripts/price-monitor.exe run-adapter data/new.csv --provider new --adapter adapters/new/adapter.py --output output-full/new --resume
```

Не используйте resume после изменения адаптера/исходника: старые завершённые
записи будут пропущены. При ошибках скрипт возвращает ненулевой код; успешные
записи сохраняются, причину смотрите в errors.jsonl.

## Входной формат

CSV: html или html_content для HTML, response_json для JSON, content для смешанного формата. Поддерживаются
provider, city/city_name, region/region_name, url/target_url/base_url, timestamp.
Для JSON в каждой строке хранится ответ каталога, а не путь к JSON-файлу.
Parquet тоже поддерживается, включая исходное расширение .parqeut.
Отсутствующие метаданные получают стабильные служебные значения.
