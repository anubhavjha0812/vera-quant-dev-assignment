FROM python:3.12-slim

WORKDIR /app

# Must match the Poetry version that generated poetry.lock (lock-version
# 2.1, in [metadata]) -- Poetry 1.8.x can't read a 2.x-format lock file
# and the install below would fail immediately with an older pin.
RUN pip install --no-cache-dir poetry==2.2.1 \
    && poetry config virtualenvs.create false

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --no-interaction --no-ansi

COPY . .
RUN poetry install --no-interaction --no-ansi

CMD ["python", "scripts/demo_backtest.py"]
