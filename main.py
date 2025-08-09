import sys
import os
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from mod_utils import ModUtils
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
import threading
import time
from typing import Dict
import shutil

class MainWindow:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TASK军事模拟俱乐部武装突袭重铸者模组同步器 V3.0.0")
        self.root.geometry("1000x800")  # 减小窗口大小
        
        self.setup_logging()
        self.setup_ui()
        self.is_running = True  # 添加is_running属性
        
        # 检查版本更新
        ModUtils.initialize()
        
    def setup_logging(self):
        """设置日志系统"""
        # 创建日志目录到用户文档目录
        log_dir = os.path.join(os.path.expanduser("~"), "Documents", "mod_sync_tool")
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)

        # 轮转日志：按天分割，保留7天
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        # 清理可能的旧处理器避免重复
        for h in list(logger.handlers):
            logger.removeHandler(h)

        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
        file_handler = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, 'mod_sync.log'),
            when='midnight',
            backupCount=7,
            encoding='utf-8',
            utc=False
        )
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
        
    def setup_ui(self):
        # 创建主框架
        main_frame = ttk.Frame(self.root, padding="2")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 配置网格权重
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        main_frame.rowconfigure(1, weight=0)
        main_frame.rowconfigure(2, weight=0)
        main_frame.rowconfigure(3, weight=1)
        
        # 作者信息
        author_frame = ttk.Frame(main_frame)
        author_frame.grid(row=0, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 2))
        author_frame.columnconfigure(0, weight=1)
        
        author_label = ttk.Label(author_frame, 
                               text="作者: ViVi141  QQ: 747384120", 
                               font=("微软雅黑", 8),
                               justify='center')
        author_label.grid(row=0, column=0, sticky=(tk.W, tk.E))
        
        # 模组列表选择
        modlist_frame = ttk.Frame(main_frame)
        modlist_frame.grid(row=1, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=1)
        modlist_frame.columnconfigure(1, weight=1)
        
        ttk.Label(modlist_frame, text="模组列表:").grid(row=0, column=0, sticky=tk.W, padx=1)
        self.modlist_var = tk.StringVar()
        self.modlist_combo = ttk.Combobox(modlist_frame, textvariable=self.modlist_var, state="readonly", width=40)
        self.update_modlist_options()  # 初始化模组列表选项
        self.modlist_combo.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=1)
        
        # 本地模组路径选择
        path_frame = ttk.Frame(main_frame)
        path_frame.grid(row=2, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=1)
        path_frame.columnconfigure(1, weight=1)
        
        ttk.Label(path_frame, text="本地模组路径:").grid(row=0, column=0, sticky=tk.W, padx=1)  # 进一步减小padx
        # 使用用户的文档目录
        default_path = os.path.join(os.path.expanduser("~"), "Documents", "My Games", "ArmaReforger", "addons")
        self.local_path_var = tk.StringVar(value=default_path)
        ttk.Entry(path_frame, textvariable=self.local_path_var, width=40).grid(row=0, column=1, sticky=(tk.W, tk.E), padx=1)  # 减小宽度和padx
        ttk.Button(path_frame, text="浏览", command=self.browse_local_path).grid(row=0, column=2, padx=1)  # 进一步减小padx
        
        # 节点选择
        node_frame = ttk.Frame(main_frame)
        node_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=1)
        node_frame.columnconfigure(1, weight=1)
        
        ttk.Label(node_frame, text="下载节点:").grid(row=0, column=0, sticky=tk.W, padx=1)
        self.node_var = tk.StringVar(value="自动选择")
        self.node_combo = ttk.Combobox(node_frame, textvariable=self.node_var, state="readonly", width=12)
        self.update_node_list()  # 初始化节点列表
        self.node_combo.grid(row=0, column=1, sticky=tk.W, padx=1)
        
        # 更新节点状态按钮
        ttk.Button(node_frame, text="更新节点状态", command=self.update_node_status).grid(row=0, column=2, padx=1)
        
        # 节点状态标签
        self.node_status_var = tk.StringVar(value="")
        ttk.Label(node_frame, textvariable=self.node_status_var).grid(row=0, column=3, sticky=tk.W, padx=1)
        
        # 下载速度限制控制
        speed_frame = ttk.Frame(main_frame)
        speed_frame.grid(row=4, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=1)
        speed_frame.columnconfigure(1, weight=1)
        
        ttk.Label(speed_frame, text="下载速度限制:").grid(row=0, column=0, sticky=tk.W, padx=1)
        
        # 速度限制滑动条
        self.speed_var = tk.DoubleVar(value=0)
        self.speed_scale = ttk.Scale(
            speed_frame,
            from_=0,
            to=1000,
            orient=tk.HORIZONTAL,
            variable=self.speed_var,
            command=self.on_speed_change
        )
        self.speed_scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=1)
        self.speed_scale.bind("<ButtonRelease-1>", self.on_speed_change_final)
        
        # 速度限制输入框
        self.speed_entry = ttk.Entry(speed_frame, width=8)
        self.speed_entry.grid(row=0, column=2, padx=1)
        self.speed_entry.insert(0, "0")
        self.speed_entry.bind('<Return>', self.on_speed_entry_change)
        self.speed_entry.bind('<FocusOut>', self.on_speed_entry_change)
        
        ttk.Label(speed_frame, text="Mbps").grid(row=0, column=3, sticky=tk.W, padx=1)
        
        # 取消同步事件
        self.cancel_event = threading.Event()

        # 按钮框架
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=5, column=0, columnspan=3, pady=1)  # 进一步减小pady
        
        # 检查按钮
        ttk.Button(button_frame, text="检查模组", command=self.check_mods).grid(row=0, column=0, padx=1)  # 进一步减小padx
        # 同步按钮
        self.sync_btn = ttk.Button(button_frame, text="同步模组", command=self.sync_mods)
        self.sync_btn.grid(row=0, column=1, padx=1)  # 进一步减小padx
        # 暂停/继续按钮
        self.pause_event = threading.Event()
        self.pause_event.set()  # 初始为运行状态
        self.pause_btn = ttk.Button(button_frame, text="暂停", command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=2, padx=1)  # 进一步减小padx
        # 取消同步按钮
        self.cancel_btn = ttk.Button(button_frame, text="取消同步", command=self.cancel_sync, state='disabled')
        self.cancel_btn.grid(row=0, column=3, padx=1)  # 进一步减小padx
        
        # 创建模组列表和日志的容器
        content_frame = ttk.Frame(main_frame)
        content_frame.grid(row=6, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=1)  # 进一步减小pady
        content_frame.columnconfigure(0, weight=3)  # 减小模组列表的权重
        content_frame.columnconfigure(1, weight=2)  # 增加日志区域的权重
        content_frame.rowconfigure(0, weight=1)
        
        # 创建模组列表
        self.create_mod_list(content_frame)
        
        # 创建日志界面
        self.create_log_view(content_frame)
        
        # 进度条
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, length=980, mode='determinate', variable=self.progress_var)  # 减小长度
        self.progress_bar.grid(row=7, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=1)  # 进一步减小pady

        # 下载速度和预计时间标签
        self.speed_eta_label = ttk.Label(main_frame, text="", font=("微软雅黑", 7))  # 进一步减小字体
        self.speed_eta_label.grid(row=8, column=0, columnspan=3, sticky=tk.W, pady=(0, 1))

        # 状态栏
        self.status_var = tk.StringVar()
        self.status_var.set("就绪")
        ttk.Label(main_frame, textvariable=self.status_var).grid(row=9, column=0, columnspan=3, sticky=tk.W)
        
        # 初始化节点状态
        self.update_node_status()

    def create_mod_list(self, parent):
        # 创建模组列表框架
        list_frame = ttk.LabelFrame(parent, text="模组列表", padding="1")  # 进一步减小padding
        list_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S), pady=1)  # 进一步减小pady
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        
        # 创建Treeview
        columns = ('name', 'local_version', 'remote_version', 'status', 'progress', 'rate')
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=25)  # 增加高度
        
        # 设置列标题
        self.tree.heading('name', text='模组名称', command=lambda: self.sort_treeview('name', False))
        self.tree.heading('local_version', text='本地版本', command=lambda: self.sort_treeview('local_version', False))
        self.tree.heading('remote_version', text='云端版本', command=lambda: self.sort_treeview('remote_version', False))
        self.tree.heading('status', text='状态', command=lambda: self.sort_treeview('status', False))
        self.tree.heading('progress', text='进度', command=lambda: self.sort_treeview('progress', False))
        self.tree.heading('rate', text='速度/剩余', command=lambda: self.sort_treeview('rate', False))
        
        # 设置列宽
        self.tree.column('name', width=250)  # 减小宽度
        self.tree.column('local_version', width=80)  # 减小宽度
        self.tree.column('remote_version', width=80)  # 减小宽度
        self.tree.column('status', width=100)  # 减小宽度
        self.tree.column('progress', width=80)
        self.tree.column('rate', width=180)
        
        # 添加滚动条
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        # 放置组件
        self.tree.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        
        # 添加右键菜单
        self.create_context_menu()

        # 配置行样式（高亮失败/成功）
        try:
            self.tree.tag_configure('failed', foreground='red')
            self.tree.tag_configure('synced', foreground='green')
        except Exception:
            pass
        
    def create_context_menu(self):
        """创建右键菜单"""
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="按名称排序", command=lambda: self.sort_treeview('name', False))
        self.context_menu.add_command(label="按本地版本排序", command=lambda: self.sort_treeview('local_version', False))
        self.context_menu.add_command(label="按云端版本排序", command=lambda: self.sort_treeview('remote_version', False))
        self.context_menu.add_command(label="按状态排序", command=lambda: self.sort_treeview('status', False))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="打开选中模组目录", command=self.open_selected_mod_folder)
        self.context_menu.add_command(label="打开日志目录", command=self.open_log_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="反向排序", command=self.reverse_sort)
        
        # 绑定右键菜单
        self.tree.bind("<Button-3>", self.show_context_menu)
        
    def show_context_menu(self, event):
        """显示右键菜单"""
        self.context_menu.post(event.x_root, event.y_root)

    def open_log_folder(self):
        try:
            log_dir = os.path.join(os.path.expanduser("~"), "Documents", "mod_sync_tool")
            if os.path.exists(log_dir):
                os.startfile(log_dir)
        except Exception as e:
            self.log_message(f"打开日志目录失败: {str(e)}", logging.ERROR)

    def open_selected_mod_folder(self):
        try:
            selection = self.tree.selection()
            if not selection:
                return
            mod_id = selection[0]
            values = list(self.tree.item(mod_id).get('values', []))
            name = values[0] if values else mod_id
            base = self.local_path_var.get()
            # 首选直接用 name 作为目录名
            candidates = [os.path.join(base, name), os.path.join(base, f"{name}_{mod_id}")]
            for p in candidates:
                if os.path.exists(p):
                    os.startfile(p)
                    return
            # 若都不存在，打开基目录
            if os.path.exists(base):
                os.startfile(base)
        except Exception as e:
            self.log_message(f"打开模组目录失败: {str(e)}", logging.ERROR)
        
    def sort_treeview(self, col, reverse):
        """排序列表"""
        # 获取所有项目
        items = [(self.tree.set(item, col), item) for item in self.tree.get_children('')]
        
        # 定义排序规则
        def sort_key(item):
            value = item[0]
            if col in ['local_version', 'remote_version']:
                # 版本号排序
                if value == '未安装' or value == 'unknown':
                    return (0, '')  # 未安装和未知版本排在最前
                try:
                    # 尝试将版本号转换为数字进行比较
                    return (1, [int(x) for x in value.split('.')])
                except:
                    return (2, value)  # 无法转换的版本号排在最后
            elif col == 'status':
                # 状态排序
                status_order = {
                    '未安装': 0,
                    '云端未知': 1,
                    '需要更新': 2,
                    '已同步': 3,
                    '更新中...': 4,
                    '下载中...': 4,
                    '解压失败': 5,
                    '下载失败': 5
                }
                return (status_order.get(value, 999), value)
            elif col in ['progress']:
                # 进度百分比排序，形如 '12.3%'
                try:
                    return (0, float(value.strip('% ')))
                except Exception:
                    return (1, 0.0)
            else:
                # 其他列按字符串排序
                return (0, value.lower())
        
        # 排序
        items.sort(key=sort_key, reverse=reverse)
        
        # 重新排列项目
        for index, (_, item) in enumerate(items):
            self.tree.move(item, '', index)
            
        # 切换排序方向
        self.tree.heading(col, command=lambda: self.sort_treeview(col, not reverse))
        
    def reverse_sort(self):
        """反向排序当前列"""
        # 获取当前排序的列
        for col in ['name', 'local_version', 'remote_version', 'status']:
            if self.tree.heading(col)['text'].endswith(' ↓'):
                self.sort_treeview(col, True)
                break
            elif self.tree.heading(col)['text'].endswith(' ↑'):
                self.sort_treeview(col, False)
                break
        
    def browse_local_path(self):
        path = filedialog.askdirectory()
        if path:
            self.local_path_var.set(path)
            
    def create_log_view(self, parent):
        """创建日志显示界面"""
        # 创建日志框架
        log_frame = ttk.LabelFrame(parent, text="操作日志", padding="1")  # 进一步减小padding
        log_frame.grid(row=0, column=1, sticky=(tk.W, tk.E, tk.N, tk.S), padx=(1, 0))  # 进一步减小padx
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        
        # 创建日志文本框
        self.log_text = tk.Text(log_frame, wrap=tk.WORD, width=30, height=25)  # 减小宽度，增加高度
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
        # 添加滚动条
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)
        log_scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))
        
        # 设置日志文本框只读
        self.log_text.configure(state='disabled')
        
        # 创建自定义日志处理器
        self.log_handler = LogHandler(self.log_text)
        logging.getLogger().addHandler(self.log_handler)
        
    def log_message(self, message, level=logging.INFO):
        """添加日志消息"""
        logging.log(level, message)
        
    def check_mods(self):
        """检查模组状态"""
        local_path = self.local_path_var.get()
        if not local_path:
            messagebox.showerror("错误", "请选择本地模组路径")
            return
            
        try:
            # 清空列表
            for item in self.tree.get_children():
                self.tree.delete(item)
                
            # 获取选择的模组列表
            selected_list = self.modlist_var.get()
            if not selected_list:
                messagebox.showerror("错误", "请选择模组列表")
                return
                
            # 获取本地模组
            self.log_message("开始扫描本地模组...")
            self.status_var.set("正在扫描本地模组...")
            self.progress_var.set(10)
            try:
                local_mods = ModUtils.get_local_mods(local_path)
                self.log_message(f"本地模组扫描完成，共发现 {len(local_mods)} 个模组")
            except Exception as e:
                error_msg = str(e)
                if "权限错误" in error_msg:
                    if messagebox.askretrycancel("权限错误", 
                        f"无法访问模组目录: {error_msg}\n\n"
                        "请尝试以下方法：\n"
                        "1. 以管理员身份运行程序\n"
                        "2. 检查目录权限设置\n"
                        "3. 选择其他目录\n\n"
                        "是否重试？"):
                        self.check_mods()
                    return
                raise
            
            # 获取远程模组
            self.log_message("正在连接云端服务器...")
            self.status_var.set("正在连接云端服务器...")
            self.progress_var.set(30)
            remote_mods = ModUtils.get_remote_mods(selected_list)  # 传入选择的模组列表
            self.log_message(f"云端模组获取完成，共发现 {len(remote_mods)} 个模组")
            
            # 比较模组
            self.log_message("正在比较模组版本...")
            self.status_var.set("正在比较模组版本...")
            self.progress_var.set(50)
            to_update, to_add, to_remove = ModUtils.compare_mods(local_mods, remote_mods)
            
            # 更新列表
            for mod_id, info in local_mods.items():
                remote_info = remote_mods.get(mod_id, {})
                remote_version = remote_info.get('version', 'unknown')
                status = ModUtils.compare_versions(info['version'], remote_version)
                self.tree.insert('', 'end', iid=mod_id, values=(
                    info['name'],
                    info['version'],
                    remote_version,
                    status
                ))
                
            for mod_id in to_add:
                info = remote_mods[mod_id]
                status = ModUtils.compare_versions('', info['version'])
                self.tree.insert('', 'end', iid=mod_id, values=(
                    info['name'],
                    '未安装',
                    info['version'],
                    status
                ))
                
            # 显示检查结果
            update_count = len(to_update)
            add_count = len(to_add)
            
            if update_count > 0 or add_count > 0:
                self.status_var.set(f"检查完成（{update_count}个需要更新，{add_count}个需要下载）")
                self.log_message(f"检查完成：\n- {update_count} 个模组需要更新\n- {add_count} 个模组需要下载")
            else:
                self.status_var.set("所有模组已是最新版本")
                self.log_message("所有模组已是最新版本")
                
            self.progress_var.set(100)
                    
        except Exception as e:
            error_msg = str(e)
            self.log_message(f"检查失败: {error_msg}", logging.ERROR)
            messagebox.showerror("错误", error_msg)
            self.status_var.set("检查失败")
            self.progress_var.set(0)

    def cancel_sync(self):
        self.cancel_event.set()
        self.status_var.set("已取消同步，正在清理缓存...")
        self.log_message("用户取消了同步，正在清理缓存文件...")
        self.cleanup_all_tmp_files()
        self.status_var.set("同步已取消，缓存已清理")
        self.cancel_btn['state'] = 'disabled'
        self.sync_btn['state'] = 'normal'
        self.is_syncing = False

    def sync_mods(self):
        """同步模组"""
        if not self.local_path_var.get():
            messagebox.showerror("错误", "请选择本地模组路径")
            return
            
        # 获取选择的模组列表
        selected_list = self.modlist_var.get()
        if not selected_list:
            messagebox.showerror("错误", "请选择模组列表")
            return
            
        # 获取选择的节点
        preferred_node = self.get_selected_node()
        if preferred_node:
            logging.info(f"使用节点: {preferred_node}")
            
        # 禁用同步按钮
        self.sync_btn.configure(state='disabled')
        # 启用取消按钮
        self.cancel_btn.configure(state='normal')
        # 重置取消事件
        self.cancel_event.clear()
        
        # 清空进度条
        self.progress_var.set(0)
        self.speed_eta_label.configure(text="")
        
        # 更新状态
        self.status_var.set("正在同步...")
        
        # 在新线程中执行同步
        def sync_thread():
            try:
                # 获取本地模组列表
                local_mods = ModUtils.get_local_mods(self.local_path_var.get())
                
                # 获取远程模组列表
                remote_mods = ModUtils.get_remote_mods(selected_list)  # 传入选择的模组列表
                
                # 比较模组
                to_update, to_add, to_remove = ModUtils.compare_mods(local_mods, remote_mods)

                # 磁盘空间预检
                all_targets = to_update + to_add
                if all_targets:
                    try:
                        size_map = ModUtils.prefetch_sizes(all_targets, selected_list)
                    except Exception:
                        size_map = {}
                    total_need = sum(int(size_map.get(mid, 0) or 0) for mid in all_targets)
                    try:
                        usage = shutil.disk_usage(self.local_path_var.get())
                        free_bytes = usage.free
                    except Exception:
                        free_bytes = 0
                    # 预留 1GB 余量
                    reserve = 1 * 1024 * 1024 * 1024
                    if free_bytes and total_need and total_need > max(0, free_bytes - reserve):
                        msg = (
                            f"空间不足：预计需要 {total_need/1024/1024/1024:.2f} GB，"
                            f"当前可用 {free_bytes/1024/1024/1024:.2f} GB。请清理磁盘后重试。"
                        )
                        logging.error(msg)
                        messagebox.showerror("空间不足", msg)
                        return
                
                if not to_update and not to_add:
                    messagebox.showinfo("提示", "所有模组已是最新版本")
                    return
                    
                # 处理模组
                results = self.process_mods(
                    self.local_path_var.get(),
                    to_update,
                    to_add,
                    local_mods,
                    remote_mods,
                    preferred_node
                )
                
                # 显示结果（增强原因统计）
                success_count = sum(1 for status in results.values() if status == "成功")
                failed_count = len(results) - success_count
                failed_mods = {mod_id: status for mod_id, status in results.items() if status != "成功"}
                # 统计权限相关失败
                perm_fails = [m for m, s in failed_mods.items() if isinstance(s, str) and ("权限" in s or "访问" in s)]
                
                self.show_sync_result(success_count, failed_count, failed_mods)
                if perm_fails:
                    logging.warning("检测到权限相关失败的模组：" + ", ".join(perm_fails))
                    logging.warning("建议以管理员身份运行或检查目标目录NTFS权限与安全软件拦截")
                
            except Exception as e:
                messagebox.showerror("错误", f"同步失败: {str(e)}")
                logging.error(f"同步失败: {str(e)}")
            finally:
                # 恢复按钮状态
                self.sync_btn.configure(state='normal')
                self.cancel_btn.configure(state='disabled')
                self.status_var.set("就绪")
                
        threading.Thread(target=sync_thread, daemon=True).start()

    def process_mods(self, local_path, to_update, to_add, local_mods, remote_mods, preferred_node=None):
        """处理模组更新和下载"""
        results = {}
        
        # 获取当前选择的模组列表
        selected_list = self.modlist_var.get()
        
        def process_in_thread():
            try:
                # 获取选择的节点
                node = preferred_node
                if node:
                    logging.info(f"使用指定节点: {node}")

                # 并行处理下载与解压
                # 进度聚合：总体进度 + 当前文件
                aggregate = {
                    'mod_bytes': {},    # mid -> downloaded bytes
                    'mod_totals': {},   # mid -> total bytes
                    'speeds': {},       # mid -> bytes/s
                }
                last_ui_update = {'t': 0.0}
                MIN_UPDATE_INTERVAL = 0.2  # 秒，限流UI刷新，避免闪烁

                def download_callback(mid, p, s=0, e=0, cur_bytes=0, total_bytes=0):
                    if self.cancel_event.is_set():
                        return False
                    # 更新分项数据
                    if total_bytes and total_bytes > 0:
                        aggregate['mod_totals'][mid] = total_bytes
                    aggregate['mod_bytes'][mid] = max(cur_bytes, aggregate['mod_bytes'].get(mid, 0))
                    aggregate['speeds'][mid] = s
                    # 更新表格该行的“进度/速度/ETA”
                    try:
                        item = mid
                        row = self.tree.item(item)
                        if row and row.get('values'):
                            values = list(row['values'])
                            # 确保列索引存在：name, local_version, remote_version, status, progress, rate
                            while len(values) < 6:
                                values.append("")
                            values[4] = f"{p:.1f}%"
                            speed_text = f"{s/1024/1024:.1f} MB/s"
                            eta_text = f"{e/60:.1f} 分钟" if e > 0 else "计算中..."
                            values[5] = f"{speed_text} / {eta_text}"
                            self.tree.item(item, values=values)
                    except Exception:
                        pass

                    # 计算总体进度
                    overall_total = sum(aggregate['mod_totals'].values()) or 0
                    overall_done = 0
                    for k, v in aggregate['mod_bytes'].items():
                        overall_done += min(v, aggregate['mod_totals'].get(k, v))

                    if overall_total > 0:
                        overall_progress = overall_done / overall_total * 100.0
                    else:
                        overall_progress = p  # 退化到当前文件

                    # 计算总体速度：仅汇总活跃任务（最近1秒有增量）
                    combined_speed = 0.0
                    # 仅统计仍在进行的任务速度（根据 done/total 判定），避免已完成任务残留速度
                    for mid_k, total in aggregate['mod_totals'].items():
                        done = aggregate['mod_bytes'].get(mid_k, 0)
                        spd = aggregate['speeds'].get(mid_k, 0.0) or 0.0
                        if done < total and spd > 0:
                            combined_speed += spd
                    remaining = max(overall_total - overall_done, 0)
                    overall_eta = (remaining / combined_speed) if combined_speed > 0 else 0

                    # 节流 UI 更新并切回主线程
                    import time as _t
                    now = _t.time()
                    if now - last_ui_update['t'] < MIN_UPDATE_INTERVAL:
                        return True
                    last_ui_update['t'] = now

                    def do_update():
                        try:
                            # 总进度取聚合值，但不将“下载完成但未解压”的任务当作完全完成；此处仅显示下载层面的聚合
                            self.progress_var.set(overall_progress)
                            overall_speed_text = f"{combined_speed/1024/1024:.1f} MB/s"
                            overall_eta_text = f"{overall_eta/60:.1f} 分钟" if overall_eta > 0 else "计算中..."
                            current_speed_text = f"{s/1024/1024:.1f} MB/s"
                            current_eta_text = f"{e/60:.1f} 分钟" if e > 0 else "计算中..."
                            self.status_var.set(
                                f"总体 {overall_progress:.1f}% ({overall_speed_text}, 剩余 {overall_eta_text}) | "
                                f"当前 {mid}: {p:.1f}% ({current_speed_text}, 剩余 {current_eta_text})"
                            )
                            total_mods = max(len(aggregate['mod_totals']), 1)
                            done_mods = sum(1 for k in aggregate['mod_totals'] if aggregate['mod_bytes'].get(k, 0) >= aggregate['mod_totals'].get(k, 1))
                            self.speed_eta_label.configure(text=f"模组进度: {done_mods}/{total_mods}")
                        except Exception:
                            pass
                    try:
                        self.root.after(0, do_update)
                    except Exception:
                        do_update()
                    return True

                # 下载前预取所有目标文件大小用于总体进度
                # 构建候选模组清单
                all_targets = to_update + to_add
                # 预取大小
                try:
                    size_map = ModUtils.prefetch_sizes(all_targets, selected_list)
                except Exception:
                    size_map = {}

                # 初始化每项总大小到聚合器
                for mid in all_targets:
                    total_bytes = size_map.get(mid, 0)
                    if total_bytes > 0:
                        aggregate['mod_totals'][mid] = total_bytes

                parallel_results = ModUtils.process_mods_parallel(
                    local_path,
                    to_update,
                    to_add,
                    local_mods,
                    remote_mods,
                    progress_callback=download_callback,
                    pause_event=self.pause_event
                )

                # 汇总结果并更新 UI
                for mod_id, status in parallel_results.items():
                    results[mod_id] = status
                    self.update_tree_item(mod_id, status)

            except Exception as e:
                logging.error(f"处理模组失败: {str(e)}")
                raise
                
        # 在新线程中执行处理
        thread = threading.Thread(target=process_in_thread)
        thread.start()
        thread.join()
        
        return results

    def show_sync_result(self, success_count, failed_count, failed_mods):
        self.progress_var.set(100)
        
        # 显示同步结果
        if failed_count > 0:
            self.status_var.set(f"同步完成（{success_count}成功，{failed_count}失败）")
            result_message = f"同步完成\n\n成功: {success_count} 个模组\n失败: {failed_count} 个模组\n\n失败的模组：\n"
            for mod_id, status in failed_mods.items():
                mod_name = self.get_mod_name(mod_id)
                error = status
                result_message += f"- {mod_name}: {error}\n"
            self.log_message(result_message, logging.WARNING)
            messagebox.showwarning("同步结果", result_message)
        else:
            self.status_var.set("同步完成")
            self.log_message(f"模组同步已完成，共成功同步 {success_count} 个模组")
            messagebox.showinfo("完成", f"模组同步已完成，共成功同步 {success_count} 个模组")
        self.is_syncing = False
        self.sync_btn['state'] = 'normal'
        self.cancel_btn['state'] = 'disabled'
        self.cleanup_all_tmp_files()
            
    def get_mod_name(self, mod_id):
        """获取模组名称"""
        try:
            values = list(self.tree.item(mod_id)['values'])
            return values[0] if values else mod_id
        except Exception:
            return mod_id

    def update_tree_item(self, mod_id, status):
        try:
            values = list(self.tree.item(mod_id)['values'])
            if values:
                display_status = '已同步' if status == '成功' else status
                values[3] = display_status
                tags = ()
                if isinstance(display_status, str):
                    if display_status in ("解压失败", "下载失败") or display_status.startswith("失败") or display_status.endswith("失败"):
                        tags = ('failed',)
                    elif display_status == '已同步':
                        tags = ('synced',)
                self.tree.item(mod_id, values=values, tags=tags)
        except Exception:
            pass
                
    def run(self):
        # 绑定关闭窗口事件，执行清理
        def on_close():
            try:
                self.cancel_event.set()
                self.is_running = False
                self.cleanup_all_tmp_files()
            finally:
                self.root.destroy()
        self.root.protocol("WM_DELETE_WINDOW", on_close)
        self.root.mainloop()

    def update_node_list(self):
        """更新节点列表"""
        try:
            # 获取可用节点
            available_nodes = ModUtils.get_available_nodes()
            if not available_nodes:
                self.node_combo['values'] = ["自动选择"]
                self.node_var.set("自动选择")
                return
            
            # 构建节点选项
            node_options = ["自动选择"]
            for node_id in available_nodes:
                node_info = ModUtils.get_node_info(node_id)
                if node_info:
                    try:
                        node_name = node_info.get('name', node_id)
                        node_options.append(f"{node_name}节点")
                    except Exception as e:
                        logging.error(f"处理节点 {node_id} 名称失败: {str(e)}")
                        continue
            
            # 更新下拉列表
            self.node_combo['values'] = node_options
            
            # 如果当前选择不在新列表中，重置为自动选择
            if self.node_var.get() not in node_options:
                self.node_var.set("自动选择")
                
        except Exception as e:
            logging.error(f"更新节点列表失败: {str(e)}")
            self.node_combo['values'] = ["自动选择"]
            self.node_var.set("自动选择")

    def update_node_status(self):
        """更新节点状态"""
        try:
            available_nodes = ModUtils.get_available_nodes()
            if not available_nodes:
                self.node_status_var.set("无可用节点")
                return
                
            status_text = []
            for node_id in available_nodes:
                node_info = ModUtils.get_node_info(node_id)
                if not node_info:
                    continue
                    
                try:
                    latency, is_available = ModUtils.check_node_latency(
                        node_info['endpoint'],
                        node_info['port'],
                        samples=5,
                        method='tcp',
                        secure=bool(node_info.get('secure', False))
                    )
                    
                    node_name = node_info.get('name', node_id)
                    if is_available:
                        status_text.append(f"{node_name}: {latency:.0f}ms")
                    else:
                        status_text.append(f"{node_name}: 不可用")
                except Exception as e:
                    logging.error(f"检查节点 {node_id} 状态失败: {str(e)}")
                    continue
                    
            if status_text:
                self.node_status_var.set(" | ".join(status_text))
            else:
                self.node_status_var.set("所有节点不可用")
                
            # 同时更新节点列表
            self.update_node_list()
            
        except Exception as e:
            self.node_status_var.set("获取节点状态失败")
            logging.error(f"更新节点状态失败: {str(e)}")

    def get_selected_node(self) -> str:
        """获取选择的节点"""
        try:
            selected = self.node_var.get()
            if not selected or selected == "自动选择":
                return None
                
            # 从显示名称中提取节点ID
            selected_name = selected.replace("节点", "")  # 移除"节点"后缀
            for node_id in ModUtils.get_available_nodes():
                node_info = ModUtils.get_node_info(node_id)
                if node_info:
                    try:
                        node_name = node_info.get('name', node_id)
                        if node_name == selected_name:
                            return node_id
                    except Exception as e:
                        logging.error(f"处理节点 {node_id} 名称失败: {str(e)}")
                        continue
                        
            return None
        except Exception as e:
            logging.error(f"获取选择的节点失败: {str(e)}")
            return None

    def toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="继续")
            self.status_var.set("已暂停")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="暂停")
            self.status_var.set("继续同步中")

    def cleanup_all_tmp_files(self):
        local_path = self.local_path_var.get()
        for file in os.listdir(local_path):
            if file.endswith('.tmp'):
                try:
                    os.remove(os.path.join(local_path, file))
                except Exception as e:
                    self.log_message(f"清理缓存文件失败: {file} {str(e)}", logging.WARNING)

    def update_progress(self, progress, speed=0, eta=0):
        """更新进度条和状态"""
        if not self.is_running:
            return
            
        try:
            # 更新进度条
            self.progress_var.set(progress)
            
            # 更新状态文本
            if speed > 0:
                speed_text = f"{speed/1024/1024:.1f} MB/s"
                if eta > 0:
                    eta_text = f"{eta/60:.1f}分钟"
                else:
                    eta_text = "计算中..."
                status_text = f"正在下载: {progress:.1f}% ({speed_text}, 剩余时间: {eta_text})"
            else:
                status_text = f"正在下载: {progress:.1f}%"
            
            self.status_var.set(status_text)
            
            # 更新UI
            self.root.update_idletasks()
            
        except Exception as e:
            logging.error(f"更新进度失败: {str(e)}")
            self.log_message(f"更新进度失败: {str(e)}", logging.ERROR)

    def update_modlist_options(self):
        """更新模组列表选项"""
        modlists = ModUtils.get_mod_lists()
        if modlists:
            self.modlist_combo['values'] = list(modlists.keys())
            # 默认选择第一个选项
            if self.modlist_combo['values']:
                self.modlist_var.set(self.modlist_combo['values'][0])
        else:
            self.modlist_combo['values'] = []
            self.modlist_var.set("")

    def on_speed_change(self, value):
        """处理滑动条值变化（仅更新显示）"""
        try:
            speed = float(value)
            self.speed_entry.delete(0, tk.END)
            self.speed_entry.insert(0, f"{speed:.1f}")
        except ValueError:
            pass
            
    def on_speed_change_final(self, event=None):
        """当用户释放滑动条时应用速度设置"""
        try:
            speed = self.speed_var.get()
            # 设置速度限制（转换为MB/s）
            ModUtils.set_speed_limit(speed / 8)  # 将Mbps转换为MB/s
            self.log_message(f"下载速度限制已设置为 {speed:.1f} Mbps")
        except ValueError:
            pass

    def on_speed_entry_change(self, event=None):
        """处理输入框值变化"""
        try:
            speed = float(self.speed_entry.get())
            if speed < 0:
                speed = 0
            elif speed > 1000:
                speed = 1000
            self.speed_var.set(speed)
            # 设置速度限制（转换为MB/s）
            ModUtils.set_speed_limit(speed / 8)  # 将Mbps转换为MB/s
            self.log_message(f"下载速度限制已设置为 {speed:.1f} Mbps")
        except ValueError:
            # 如果输入无效，恢复滑动条的值
            self.speed_entry.delete(0, tk.END)
            self.speed_entry.insert(0, f"{self.speed_var.get():.1f}")

class LogHandler(logging.Handler):
    """自定义日志处理器，用于在GUI中显示日志"""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget
        
    def emit(self, record):
        msg = self.format(record)
        
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.see(tk.END)
            self.text_widget.configure(state='disabled')
            
        # 在主线程中更新GUI
        self.text_widget.after(0, append)

def cleanup_temp_files(local_path: str):
    """清理临时文件"""
    for file in os.listdir(local_path):
        if file.endswith('.7z.tmp'):
            try:
                os.remove(os.path.join(local_path, file))
            except:
                pass

def process_mod(mod_id: str, local_path: str, remote_mods: Dict[str, dict], local_mods: Dict[str, dict]) -> str:
    """处理单个模组"""
    try:
        # 下载模组
        archive_path = os.path.join(local_path, f"{mod_id}.7z")
        if ModUtils.extract_mod(archive_path, local_path):
            # 清理下载的7z文件
            try:
                os.remove(archive_path)
            except:
                pass
            return "成功"
        return "解压失败"
    except Exception as e:
        return f"处理失败: {str(e)}"

if __name__ == "__main__":
    app = MainWindow()
    app.run() 