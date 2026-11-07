> Part of the *Close the Books* repo — see [`../README.md`](../README.md) for the
> challenge scaffold and [`../CLAUDE.md`](../CLAUDE.md) for the design rationale.
> This file only covers the Python/dev environment.

# Talky

Hackathon project environment: Python 3.12, `uv`, PyTorch and Hugging Face
`transformers`, all managed by [devenv](https://devenv.sh).

## Enter the shell

```sh
devenv shell
```

or, with direnv installed:

```sh
direnv allow
```

On first entry devenv creates a virtualenv in `.devenv/state/venv` and runs
`uv sync`, which downloads PyTorch and the rest of the dependencies
(the CUDA-enabled Linux wheels are a few GB).

Check that everything works:

```sh
torch-info
```

## Daily workflow

```sh
uv add some-package          # add a dependency
uv add --dev pytest-cov      # add a dev dependency
uv remove some-package
uv sync                      # apply pyproject.toml / uv.lock changes
uv run python script.py      # run inside the managed venv
```

`uv` is forced to use the Nix-provided Python, so it never downloads its own
interpreter. The virtualenv lives in `.devenv/state/venv`, not in `.venv`.

## Hugging Face

Models and datasets are cached in `.devenv/state/huggingface`, so they stay
with the project and are easy to delete.

```sh
hf auth login                          # if a model is gated
hf download openai/whisper-small       # download a model
```

## GPU / CPU notes

- The default PyPI `torch` wheel includes CUDA support. On NixOS the host
  NVIDIA driver is exposed automatically via `/run/opengl-driver/lib`.
- No NVIDIA GPU? Uncomment the `pytorch-cpu` index block in `pyproject.toml`
  and delete `uv.lock` + `.devenv/state/venv`, then re-enter the shell.
- Optional LLM fine-tuning extras (`peft`, `trl`, `bitsandbytes`) are defined
  in `pyproject.toml`; enable them with `languages.python.uv.sync.extras` in
  `devenv.nix`.

## Useful commands

```sh
devenv test        # run enterTest (imports torch + transformers)
devenv up          # run processes, if you add any
devenv update      # update nix inputs
```

## Hackathon: bank reconciliation (`template/`)

This repo also contains a scaffold for the Talky x Anthropic *Close the Books*
hackathon (bank reconciliation at scale):

```sh
cd template
./scripts/download_data.sh    # docs + masters + one labelled month
make sample                   # build a submission
make score                    # score it with the official scorer
```

`template/README.md` documents the challenge, the scoring formula, the submission
contract, the matching policies and the two files you are meant to edit
(`src/bankrec/matching.py`, `src/bankrec/rules.py`).
