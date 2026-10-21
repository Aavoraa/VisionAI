@echo off
rem Qwen3-VL-2B reasoning model (invoice cross-checks) on the NVIDIA GPU, port 8091.
rem -ngl 99: every layer on the GPU. -fa on: flash attention. CPU fallback: see HOW_TO_RUN.txt.
cd /d "%~dp0"
tools\llama.cpp-cuda\llama-server.exe -m models\qwen3-vl-2b\Qwen3VL-2B-Instruct-Q4_K_M.gguf --mmproj models\qwen3-vl-2b\mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf --temp 0 -c 16384 -ngl 99 -fa on --host 127.0.0.1 --port 8091
