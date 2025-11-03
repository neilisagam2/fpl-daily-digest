name: FPL Daily Digest + Deadline Alerts

on:
  schedule:
    - cron: "0 * * * *"  # every hour
  workflow_dispatch:

jobs:
  fpl:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.x"

      - name: Install dependencies
        run: pip install requests

      - name: Run FPL Digest and Alerts
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          CHAT_ID: ${{ secrets.CHAT_ID }}
          FPL_TEAM_ID: ${{ secrets.FPL_TEAM_ID }}
          PYTHONIOENCODING: utf-8
        run: python fpl_deadline_notifier.py
