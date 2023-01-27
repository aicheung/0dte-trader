import tkinter as tk

class OptionChainViewer():
    def __init__(self) -> None:
        self.is_started = False
        

    def start_window(self):
        self.window = tk.Tk()

        self.text1 = tk.Label(text="Text")
        self.text1.pack()
        self.is_started = True
        self.window.mainloop()

    def update_text(self, text):
        if not self.is_started:
            return
        self.text1.config(text=text)