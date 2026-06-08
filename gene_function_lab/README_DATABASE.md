# Active Database Folder

This folder holds the SQLite database used by the Gene Function Lab website and
maintenance runner.

Expected active file:

```text
gene_function_lab.db
```

Do not rename this file during routine maintenance. The Colab runner, worker
scripts, and Hugging Face Space sync settings are configured around this
database path or its Google Drive file id.

Before a large monthly refresh, make a Drive copy of `gene_function_lab.db`.
Archive old backup copies outside this active folder so future maintainers do
not accidentally update or sync the wrong database.

