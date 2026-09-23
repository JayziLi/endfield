"""pythonw.exe -m rhodes_fast.gui_web —— 桌面快捷方式指的就是这里。

不写成 app.py 里的 `if __name__ == "__main__"`: 这个包的 __init__ 已经导入了
app, 再走 -m rhodes_fast.gui_web.app 就会把同一个模块导入两遍 (一遍叫 app, 一遍
叫 __main__)。runpy 自己会为此警告「may result in unpredictable behaviour」, 而
具体含义是 Api 有两份、模块级的 WEB_ROOT 和那几个常量也各有一份 —— 今天看不出
症状, 出症状时也不会指向这里。
"""

from .app import main

if __name__ == "__main__":
    main()
