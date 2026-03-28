# Elcut Check Web

Локальная веб-версия `ElcutCheckBot` без Telegram.

## Как запустить

Открой терминал в папке проекта и выполни:

```powershell
cd c:\Users\User\Desktop\guess-site\html
python app.py
```

После запуска открой в браузере:

```text
http://127.0.0.1:8000
```

## Что уже работает

- вход по ФИО через базу `ElcutCheckBot/database.db`
- проверка задач `1.1`, `1.2`, `1.3`, `2`, `3`
- показ статуса попыток и загрузок
- загрузка наборов файлов `.pbm`, `.mod`, `.des`

## Что использует

- база: `c:\Users\User\Desktop\guess-site\ElcutCheckBot\database.db`
- материалы: `c:\Users\User\Desktop\guess-site\ElcutCheckBot\Материалы`
