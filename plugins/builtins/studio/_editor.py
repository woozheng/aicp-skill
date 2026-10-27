"""Studio 代码编辑器 — 独立窗口 (wx 版)"""
import wx
import wx.stc as stc
import os
from pathlib import Path


class CodeEditor(wx.Frame):
    def __init__(self, parent, filepath: str, on_save=None):
        super().__init__(parent, title=f"编辑: {os.path.basename(filepath)}",
                         size=(1000, 700), style=wx.CAPTION | wx.CLOSE_BOX | wx.RESIZE_BORDER | wx.MAXIMIZE_BOX)
        self.filepath = filepath
        self.on_save = on_save

        c_bg = wx.Colour(15, 23, 42)
        c_text = wx.Colour(226, 232, 240)
        c_muted = wx.Colour(100, 116, 139)

        self.SetBackgroundColour(c_bg)
        self.SetMinSize(wx.Size(700, 500))

        panel = wx.Panel(self)
        panel.SetBackgroundColour(c_bg)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # ── 工具栏 ──
        toolbar = wx.Panel(panel, size=(-1, 40))
        toolbar.SetBackgroundColour(wx.Colour(15, 23, 42))
        tb_sizer = wx.BoxSizer(wx.HORIZONTAL)

        title = wx.StaticText(toolbar, label=f" 📄 {os.path.basename(filepath)}")
        title.SetForegroundColour(c_muted)
        tb_sizer.Add(title, 1, wx.ALIGN_CENTER | wx.LEFT, 12)

        ext = os.path.splitext(filepath)[1]
        lang_label = {"py": "PYTHON", "html": "HTML", "htm": "HTML"}.get(ext.lstrip("."), "TEXT")
        lang_colors = {"PYTHON": wx.Colour(129, 140, 248), "HTML": wx.Colour(249, 115, 22), "TEXT": c_muted}
        lang_tag = wx.StaticText(toolbar, label=f" {lang_label} ")
        lang_tag.SetBackgroundColour(lang_colors.get(lang_label, c_muted))
        lang_tag.SetForegroundColour(wx.WHITE)
        lang_tag.SetFont(lang_tag.GetFont().Bold())
        tb_sizer.Add(lang_tag, 0, wx.ALIGN_CENTER | wx.RIGHT, 12)

        toolbar.SetSizer(tb_sizer)
        sizer.Add(toolbar, 0, wx.EXPAND)

        # ── 查找栏 ──
        search_panel = wx.Panel(panel, size=(-1, 36))
        search_panel.SetBackgroundColour(c_bg)
        search_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.search_entry = wx.TextCtrl(search_panel, size=(200, -1))
        self.search_entry.SetBackgroundColour(c_bg)
        self.search_entry.SetForegroundColour(c_text)
        self.search_entry.Bind(wx.EVT_TEXT, lambda e: self._on_search_change())
        self.search_entry.Bind(wx.EVT_TEXT_ENTER, lambda e: self._do_search())
        search_sizer.Add(self.search_entry, 0, wx.ALIGN_CENTER | wx.LEFT, 4)

        for label, handler in [("🔍", self._do_search), ("◀", self._prev_result), ("▶", self._next_result)]:
            btn = wx.Button(search_panel, label=label, size=(32, 28))
            btn.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            search_sizer.Add(btn, 0, wx.ALIGN_CENTER | wx.LEFT, 2)

        self.search_label = wx.StaticText(search_panel, label="")
        self.search_label.SetForegroundColour(c_muted)
        search_sizer.Add(self.search_label, 0, wx.ALIGN_CENTER | wx.LEFT, 8)

        search_panel.SetSizer(search_sizer)
        sizer.Add(search_panel, 0, wx.EXPAND | wx.ALL, 4)

        # ── 编辑器 ──
        self.editor = stc.StyledTextCtrl(panel, style=wx.BORDER_NONE)
        self._setup_editor()
        sizer.Add(self.editor, 1, wx.EXPAND)

        # ── 底部状态栏 ──
        bottom = wx.Panel(panel, size=(-1, 40))
        bottom.SetBackgroundColour(wx.Colour(15, 23, 42))
        bt_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.status_label = wx.StaticText(bottom, label=f"行: 1, 列: 1  |  {lang_label}")
        self.status_label.SetForegroundColour(c_muted)
        bt_sizer.Add(self.status_label, 1, wx.ALIGN_CENTER | wx.LEFT, 12)

        save_btn = wx.Button(bottom, label="💾 保存 (Ctrl+S)", size=(-1, 32))
        save_btn.SetBackgroundColour(wx.Colour(34, 197, 94))
        save_btn.Bind(wx.EVT_BUTTON, lambda e: self.save())
        bt_sizer.Add(save_btn, 0, wx.ALIGN_CENTER | wx.RIGHT, 8)

        del_btn = wx.Button(bottom, label="🗑 删除", size=(-1, 32))
        del_btn.SetBackgroundColour(wx.Colour(239, 68, 68))
        del_btn.SetForegroundColour(wx.WHITE)
        del_btn.Bind(wx.EVT_BUTTON, lambda e: self._delete())
        bt_sizer.Add(del_btn, 0, wx.ALIGN_CENTER | wx.RIGHT, 12)

        bottom.SetSizer(bt_sizer)
        sizer.Add(bottom, 0, wx.EXPAND)

        panel.SetSizer(sizer)

        # ── 加载文件 ──
        try:
            self.editor.LoadFile(filepath)
        except Exception:
            self.editor.SetText(f"# Failed to load: {filepath}")

        # 设置语言
        self._set_language(ext)

        self.Bind(wx.EVT_CLOSE, lambda e: self.close())
        self.Bind(stc.EVT_STC_UPDATEUI, self._on_update_ui)
        self.editor.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.editor.Bind(wx.EVT_CHAR, self._on_char)

        self.Show()
        self.Raise()
        self.Iconize(False)
        self.SetFocus()
        self.RequestUserAttention()

        # 初始化搜索结果
        self._search_positions = []
        self._search_index = -1

    def _setup_editor(self):
        editor = self.editor

        # 样式
        editor.StyleSetSpec(stc.STC_STYLE_DEFAULT, "fore:#e2e8f0,back:#0f172a,face:Consolas,size:12")
        editor.StyleClearAll()

        # 行号
        editor.SetMarginType(0, stc.STC_MARGIN_NUMBER)
        editor.SetMarginWidth(0, 48)
        editor.StyleSetForeground(stc.STC_STYLE_LINENUMBER, wx.Colour(100, 116, 139))
        editor.StyleSetBackground(stc.STC_STYLE_LINENUMBER, wx.Colour(10, 15, 26))

        # 缩进
        editor.SetIndent(4)
        editor.SetTabWidth(4)
        editor.SetUseTabs(False)
        editor.SetTabIndents(True)
        editor.SetBackSpaceUnIndents(True)

        # 选择颜色
        editor.SetSelBackground(True, wx.Colour(71, 85, 105))
        editor.SetSelForeground(True, wx.WHITE)

        # 折叠
        editor.SetMarginType(1, stc.STC_MARGIN_SYMBOL)
        editor.SetMarginWidth(1, 16)
        editor.SetMarginSensitive(1, True)
        editor.SetProperty("fold", "1")

        # 折叠标记颜色
        c_fold_fg = wx.WHITE
        c_fold_bg = wx.Colour(60, 70, 90)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDER, stc.STC_MARK_BOXPLUS, c_fold_fg, c_fold_bg)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDEROPEN, stc.STC_MARK_BOXMINUS, c_fold_fg, c_fold_bg)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDEREND, stc.STC_MARK_BOXPLUSCONNECTED, c_fold_fg, c_fold_bg)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDEROPENMID, stc.STC_MARK_BOXMINUSCONNECTED, c_fold_fg, c_fold_bg)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDERSUB, stc.STC_MARK_VLINE, c_fold_fg, c_fold_bg)
        editor.MarkerDefine(stc.STC_MARKNUM_FOLDERTAIL, stc.STC_MARK_LCORNER, c_fold_fg, c_fold_bg)

        # 搜索标记
        editor.MarkerDefine(0, stc.STC_MARK_BACKGROUND, wx.Colour(251, 191, 36), wx.BLACK)

    def _set_language(self, ext):
        editor = self.editor
        lex = {".py": stc.STC_LEX_PYTHON, ".html": stc.STC_LEX_HTML, ".htm": stc.STC_LEX_HTML}.get(ext)
        if lex:
            editor.SetLexer(lex)

            # Python 关键字
            if lex == stc.STC_LEX_PYTHON:
                editor.SetKeyWords(0, " ".join([
                    "def class import from async await return if elif else for while try except finally",
                    "with as in not and or is None True False self pass break continue yield raise",
                    "global nonlocal lambda assert"
                ]))
                editor.StyleSetForeground(stc.STC_P_WORD, wx.Colour(192, 132, 252))
                editor.StyleSetForeground(stc.STC_P_STRING, wx.Colour(74, 222, 128))
                editor.StyleSetForeground(stc.STC_P_COMMENTLINE, wx.Colour(100, 116, 139))
                editor.StyleSetForeground(stc.STC_P_NUMBER, wx.Colour(251, 191, 36))
                editor.StyleSetForeground(stc.STC_P_DECORATOR, wx.Colour(251, 146, 60))
                editor.StyleSetForeground(stc.STC_P_DEFNAME, wx.Colour(96, 165, 250))
                editor.StyleSetForeground(stc.STC_P_CLASSNAME, wx.Colour(244, 114, 182))
                editor.StyleSetBold(stc.STC_P_DEFNAME, True)
                editor.StyleSetBold(stc.STC_P_CLASSNAME, True)

    def _on_update_ui(self, event):
        pos = self.editor.GetCurrentPos()
        line = self.editor.LineFromPosition(pos) + 1
        col = self.editor.GetColumn(pos) + 1
        self.status_label.SetLabel(f"行: {line}, 列: {col}")

    def _on_key(self, event):
        key = event.GetKeyCode()
        mod = event.GetModifiers()

        if mod == wx.MOD_CONTROL:
            if key == ord('S'):
                self.save()
                return
            if key == ord('F'):
                self.search_entry.SetFocus()
                return
            if key == ord('A'):
                self.editor.SelectAll()
                return
            if key in (ord('+'), ord('=')):
                self._zoom_in()
                return
            if key == ord('-'):
                self._zoom_out()
                return

        if mod == wx.MOD_RAW_CONTROL:
            event.Skip()
            return

        event.Skip()

    def _on_char(self, event):
        char = chr(event.GetKeyCode()) if event.GetKeyCode() < 256 else ""
        if char in "([{\"'":
            pairs = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
            self.editor.ReplaceSelection(char + pairs[char])
            pos = self.editor.GetCurrentPos()
            self.editor.SetCurrentPos(pos - 1)
            self.editor.SetSelection(pos - 1, pos - 1)
            return
        if char in ")]}":
            next_char = chr(self.editor.GetCharAt(self.editor.GetCurrentPos()))
            if next_char == char:
                self.editor.CharRight()
            return
        event.Skip()

    def _on_search_change(self):
        self.editor.MarkerDeleteAll(0)
        self._search_positions = []
        self._search_index = -1
        self.search_label.SetLabel("")

    def _do_search(self):
        query = self.search_entry.GetValue()
        self.editor.MarkerDeleteAll(0)
        self._search_positions = []
        self._search_index = -1

        if not query:
            self.search_label.SetLabel("")
            return

        self.editor.SetTargetStart(0)
        self.editor.SetTargetEnd(self.editor.GetLength())
        self.editor.SetSearchFlags(stc.STC_FIND_MATCHCASE)

        while True:
            pos = self.editor.SearchInTarget(query)
            if pos == -1:
                break
            self.editor.MarkerAdd(self.editor.LineFromPosition(pos), 0)
            self._search_positions.append(pos)
            self.editor.SetTargetStart(pos + len(query))
            self.editor.SetTargetEnd(self.editor.GetLength())

        if self._search_positions:
            self._search_index = 0
            self._goto_search_result()
            self.search_label.SetLabel(f"1/{len(self._search_positions)}")
        else:
            self.search_label.SetLabel("0/0")

    def _prev_result(self):
        if not self._search_positions:
            return
        self._search_index = (self._search_index - 1) % len(self._search_positions)
        self._goto_search_result()
        self.search_label.SetLabel(f"{self._search_index + 1}/{len(self._search_positions)}")

    def _next_result(self):
        if not self._search_positions:
            return
        self._search_index = (self._search_index + 1) % len(self._search_positions)
        self._goto_search_result()
        self.search_label.SetLabel(f"{self._search_index + 1}/{len(self._search_positions)}")

    def _goto_search_result(self):
        pos = self._search_positions[self._search_index]
        self.editor.GotoPos(pos)
        self.editor.SetSelection(pos, pos + len(self.search_entry.GetValue()))

    def _zoom_in(self):
        size = self.editor.GetZoom() + 1
        if size <= 10:
            self.editor.SetZoom(size)

    def _zoom_out(self):
        size = self.editor.GetZoom() - 1
        if size >= -6:
            self.editor.SetZoom(size)

    def save(self):
        try:
            self.editor.SaveFile(self.filepath)
            if self.on_save:
                self.on_save(self.filepath)
            old = self.status_label.GetLabel()
            self.status_label.SetLabel(f"✓ 已保存: {os.path.basename(self.filepath)}")
            wx.CallLater(1500, lambda: self.status_label.SetLabel(old))
        except Exception as e:
            wx.MessageBox(f"保存失败: {e}", "错误", wx.OK | wx.ICON_ERROR)

    def _delete(self):
        filename = os.path.basename(self.filepath)
        dlg = wx.MessageDialog(self, f"确定要删除文件\n\n{filename}\n\n此操作不可恢复！",
                               "确认删除", wx.YES_NO | wx.ICON_WARNING)
        if dlg.ShowModal() != wx.ID_YES:
            dlg.Destroy()
            return
        dlg.Destroy()

        try:
            os.remove(self.filepath)
            if self.on_save:
                self.on_save(self.filepath)
            self.Close()
        except Exception as e:
            wx.MessageBox(f"删除失败: {e}", "错误", wx.OK | wx.ICON_ERROR)

    def close(self):
        if self.on_save:
            self.on_save(self.filepath)
        self.Destroy()