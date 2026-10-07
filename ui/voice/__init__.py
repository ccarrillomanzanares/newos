"""Voz de AgentOS: Gemini Live (speech-to-speech).

El modelo oye y habla por si mismo, asi que no se necesita ni STT
(faster-whisper) ni TTS (piper). Modulos:
  - gemini_live.py  : cliente de la Live API (WebSocket)
  - gemini_listen.py: modo micro (push-to-talk o escucha continua)
"""
