@echo off
echo 正在启动B站扫码登录程序...
echo 请稍候...

:: 激活虚拟环境并运行脚本
call venv\Scripts\activate.bat
python login_bilibili.py

:: 运行结束后暂停，方便查看结果
pause

:: 退出虚拟环境（可选）
deactivate