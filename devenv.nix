{ pkgs, config, ... }:

{
  # Python 3.12 + uv-managed virtualenv.
  # 3.12 is the safest choice for current PyTorch / transformers wheels.
  # Using nixpkgs' cached build instead of nixpkgs-python (source builds).
  languages.python = {
    enable = true;
    package = pkgs.python312;
    venv.enable = true;

    # uv reads pyproject.toml / uv.lock and creates the venv on shell entry.
    uv = {
      enable = true;
      sync.enable = true;
      # Uncomment to install optional extras from pyproject.toml:
      # sync.extras = [ "llm" ];
    };

    # Compatibility libs for prebuilt manylinux wheels (torch, tokenizers, ...).
    manylinux.enable = true;
  };

  # Native libraries needed by transformers pipelines and wheel builds.
  packages = with pkgs; [
    git
    git-lfs # for cloning Hugging Face model repos
    curl
    ffmpeg-headless # audio/video decoding for transformers pipelines
    pkg-config
    openssl
    zlib
    libsndfile
  ];

  env = {
    # Keep model/dataset downloads inside the project's .devenv state dir.
    HF_HOME = config.env.DEVENV_STATE + "/huggingface";
    HF_XET_HIGH_PERFORMANCE = "1";
    TOKENIZERS_PARALLELISM = "false";
    PYTHONUNBUFFERED = "1";
  };

  # NixOS: expose the host NVIDIA driver to CUDA-enabled PyTorch wheels.
  # No-op on machines without the NixOS driver directory.
  enterShell = ''
    if [ -d /run/opengl-driver/lib ]; then
      export LD_LIBRARY_PATH="/run/opengl-driver/lib''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
  '';

  scripts = {
    # Quick sanity check: `torch-info`
    torch-info.exec = ''
      python - <<'PY'
      import sys

      import torch
      import transformers

      print(f"python       {sys.version.split()[0]}")
      print(f"torch        {torch.__version__}")
      print(f"transformers {transformers.__version__}")
      print(f"cuda         {torch.cuda.is_available()}")
      if torch.cuda.is_available():
          print(f"device       {torch.cuda.get_device_name(0)}")
      PY
    '';
  };

  enterTest = ''
    python -c "import torch, transformers; print(torch.__version__, transformers.__version__)"
  '';

  cachix.pull = [ "devenv" ];
}
