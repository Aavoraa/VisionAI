@echo off
rem PaddleOCR-VL 1.6 on the NVIDIA GPU (CUDA build of llama.cpp), port 8090.
rem -ngl 99: every layer on the GPU. -fa on: flash attention. CPU fallback: see HOW_TO_RUN.txt.
cd /d "%~dp0"
tools\llama.cpp-cuda\llama-server.exe -m models\paddleocr-vl-1.6\PaddleOCR-VL-1.6-GGUF.gguf --mmproj models\paddleocr-vl-1.6\PaddleOCR-VL-1.6-GGUF-mmproj.gguf --temp 0 -c 16384 -ngl 99 -fa on --host 127.0.0.1 --port 8090
