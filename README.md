# Crogs Telegram Bot

![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![Ruff](https://img.shields.io/badge/style-ruff-%23cc66cc.svg?logo=ruff&logoColor=white)
![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen.svg)

Telegram Bot for every-day Holidays

## Table of Contents

- [Requirements](#requirements)
- [Before You Start](#before-you-start)
- [Quick Start](#quick-start)
- [Repository Structure](#repository-structure)

---

## Requirements

- Tested on **Fedora Linux 42**
- Requires **Python 3.13**
- All dependencies are listed in [`pyproject.toml`](./pyproject.toml)

---

## Before You Start

Install all dependencies using [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Enable pre-commit hooks for auto-formatting/linting:

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

---

## Quick Start

```bash
ngrok http 8000
```

## Repository Structure

```text
.
├── ...
│
├── .../
│   ├── .../
│   │   ├── ...
│   │   └─── ...
│   └── ...
│
├── .gitignore
├── .pre-commit-config.yaml    # Pre-commit hooks config
├── .python-version
├── pyproject.toml             # Dependency and tool config
├── uv.lock
└── README.md                  # Project documentation (this file)
```
