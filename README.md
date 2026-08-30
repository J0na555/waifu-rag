# Waifu Rag Chat Bot

Source inventory (spec pipeline step 1): `python3 scripts/source_inventory.py`
writes `reports/source_inventory.{json,md}` from `data/seed_characters.yaml`;
HTTP responses cache under `.cache/source_inventory/` (delete to force fresh runs).
