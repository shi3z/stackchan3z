# launchd templates
`../install_services.sh` renders these for your machine (paths, python, env) into `~/Library/LaunchAgents` and starts them.
Placeholders: `__REPO__`, `__PY__` (system python3), `__PY_ML__` (python with insightface/mlx_whisper), `__BOARD_IP__`,
`__TTS_API__`, `__TTS_BACKEND__`, `__VLM_URL__`, `__VLM_MODEL__`, `__OLLAMA_SSH_HOST__`.
