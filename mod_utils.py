import os
import json
import shutil
import time
import threading
import queue
import concurrent.futures
from typing import Dict, List, Tuple, Optional, Callable
import logging
from minio import Minio
from minio.error import S3Error
import py7zr
import requests
from urllib.parse import urljoin
import pkg_resources
import semver

class SensitiveFilter(logging.Filter):
    """日志过滤器，用于过滤敏感信息"""
    def __init__(self):
        super().__init__()
        # 需要过滤的敏感信息模式
        self.sensitive_patterns = [
            r'access_key\s*=\s*["\']([^"\']+)["\']',
            r'secret_key\s*=\s*["\']([^"\']+)["\']',
            r'password\s*=\s*["\']([^"\']+)["\']',
            r'token\s*=\s*["\']([^"\']+)["\']',
            r'key\s*=\s*["\']([^"\']+)["\']',
            r'credential\s*=\s*["\']([^"\']+)["\']',
        ]
        import re
        self.patterns = [re.compile(pattern) for pattern in self.sensitive_patterns]

    def filter(self, record):
        if isinstance(record.msg, str):
            # 替换敏感信息为 [REDACTED]
            for pattern in self.patterns:
                record.msg = pattern.sub(r'\1=[REDACTED]', record.msg)
        return True

# 配置日志过滤器
logger = logging.getLogger("ModSyncTool")
logger.addFilter(SensitiveFilter())

class ModUtils:
    # 版本信息
    VERSION = "2.1.2"  # 当前版本
    MIN_VERSION = "2.1.2"  # 最低支持版本
    VERSION_CHECK_URL = "https://example/version.json"  # 版本检查URL
    
    logger = logger
    # 配置服务器
    CONFIG_SERVER = {
        'base_url': 'https://example/upload',
        'endpoints': {
            'mod_lists': '/mod_lists.json',
            'minio_config': '/minio_config.json',
            'version': '/version.json'
        },
        'timeout': 10,
        'retry_count': 3,
        'retry_delay': 1,
        'cache_time': 300  # 5分钟缓存
    }

    # 配置缓存
    _config_cache = {}
    _config_cache_time = {}

    # 下载队列
    download_queue = queue.Queue()
    # 下载状态字典
    download_status = {}
    # 下载锁
    download_lock = threading.Lock()

    # 下载速度限制（字节/秒）
    DOWNLOAD_SPEED_LIMIT = 0  # 0表示不限制
    # 下载速度限制窗口（秒）
    SPEED_LIMIT_WINDOW = 1.0
    # 下载速度限制缓冲区
    _speed_limit_buffer = []
    _speed_limit_lock = threading.Lock()

    @classmethod
    def initialize(cls):
        """初始化类，检查版本并输出到日志"""
        try:
            needs_update, current_version, latest_version, message = cls.get_version_info()
            if needs_update:
                cls.logger.info(f"当前版本: {current_version}")
                cls.logger.info(f"发现新版本: {latest_version}")
                cls.logger.info(f"更新说明: {message}")
            else:
                cls.logger.info(f"当前版本: {current_version}")
                cls.logger.info(f"版本状态: {message}")
        except Exception as e:
            cls.logger.error("版本检查失败")

    @staticmethod
    def check_version() -> Tuple[bool, str, str, str]:
        """检查版本更新
        Returns:
            Tuple[bool, str, str, str]: (是否需要更新, 当前版本, 最新版本, 更新说明)
        """
        try:
            # 添加时间戳和no-cdn参数避免 CDN 缓存
            timestamp = int(time.time())
            url = f"{ModUtils.VERSION_CHECK_URL}?t={timestamp}&no-cdn=1"
            
            headers = {
                'User-Agent': f'ModSyncTool/{ModUtils.VERSION}',
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'X-No-CDN': '1'
            }
            
            response = requests.get(
                url,
                timeout=ModUtils.CONFIG_SERVER['timeout'],
                headers=headers
            )
            response.raise_for_status()
            
            version_info = response.json()
            
            # 验证版本信息格式
            if not isinstance(version_info, dict):
                raise ValueError("版本信息格式错误")
                
            required_fields = ['version', 'min_version', 'download_url', 'changelog']
            missing_fields = [field for field in required_fields if field not in version_info]
            if missing_fields:
                raise ValueError(f"版本信息缺少必要字段: {', '.join(missing_fields)}")
            
            current_version = ModUtils.VERSION
            latest_version = version_info['version']
            min_version = version_info['min_version']
            
            # 检查最低版本要求
            if semver.compare(current_version, min_version) < 0:
                return True, current_version, latest_version, "当前版本过低，需要更新"
            
            # 检查是否有新版本
            if semver.compare(current_version, latest_version) < 0:
                return True, current_version, latest_version, "发现新版本"
            
            return False, current_version, latest_version, "当前已是最新版本"
            
        except requests.RequestException as e:
            logging.error("检查版本更新失败: 网络请求错误")
            return False, ModUtils.VERSION, ModUtils.VERSION, "检查更新失败"
        except ValueError as e:
            logging.error(f"检查版本更新失败: {str(e)}")
            return False, ModUtils.VERSION, ModUtils.VERSION, "版本信息格式错误"
        except Exception as e:
            logging.error("检查版本更新失败: 未知错误")
            return False, ModUtils.VERSION, ModUtils.VERSION, "检查更新失败"

    @staticmethod
    def get_version_info() -> Tuple[bool, str, str, str]:
        """获取版本信息并检查更新
        Returns:
            Tuple[bool, str, str, str]: (是否需要更新, 当前版本, 最新版本, 消息)
        """
        try:
            # 获取版本信息
            response = requests.get(
                ModUtils.VERSION_CHECK_URL,
                timeout=ModUtils.CONFIG_SERVER['timeout']
            )
            response.raise_for_status()
            version_info = response.json()
            
            current_version = ModUtils.VERSION
            latest_version = version_info.get('version', current_version)
            message = version_info.get('message', '')
            
            # 比较版本
            if latest_version > current_version:
                return True, current_version, latest_version, message
                
            return False, current_version, latest_version, "当前已是最新版本"
            
        except requests.RequestException as e:
            logging.error("检查版本更新失败: 网络请求错误")
            return False, ModUtils.VERSION, ModUtils.VERSION, "检查更新失败"
        except ValueError as e:
            logging.error(f"检查版本更新失败: {str(e)}")
            return False, ModUtils.VERSION, ModUtils.VERSION, "版本信息格式错误"
        except Exception as e:
            logging.error("检查版本更新失败: 未知错误")
            return False, ModUtils.VERSION, ModUtils.VERSION, "检查更新失败"

    @staticmethod
    def fetch_config(endpoint):
        """从配置服务器获取配置"""
        try:
            # 添加时间戳和no-cdn参数避免 CDN 缓存
            timestamp = int(time.time())
            url = f"{ModUtils.CONFIG_SERVER['base_url']}/{endpoint.lstrip('/')}?t={timestamp}&no-cdn=1"
            headers = {
                'User-Agent': 'ModSyncTool/2.0',
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'X-No-CDN': '1'  # 添加自定义头，请求不使用CDN
            }
            response = requests.get(
                url,
                timeout=ModUtils.CONFIG_SERVER['timeout'],
                headers=headers
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            ModUtils.logger.error(f"获取配置失败: {type(e).__name__} {e}")
            return None
        except ValueError as e:
            ModUtils.logger.error(f"解析配置失败: {type(e).__name__} {e}")
            return None

    @staticmethod
    def get_mod_lists() -> Dict[str, dict]:
        """获取模组列表配置
        Returns:
            Dict[str, dict]: 模组列表配置
        """
        current_time = time.time()
        # 检查缓存是否有效
        if (ModUtils._config_cache.get('mod_lists') is not None and 
            current_time - ModUtils._config_cache_time.get('mod_lists', 0) < ModUtils.CONFIG_SERVER['cache_time']):
            return ModUtils._config_cache['mod_lists']
            
        try:
            # 从服务器获取配置，确保带.json后缀
            mod_lists = ModUtils.fetch_config(ModUtils.CONFIG_SERVER['endpoints']['mod_lists'])
            if not mod_lists:
                raise ValueError("获取模组列表失败")
                
            # 验证配置格式
            if not isinstance(mod_lists, dict):
                raise ValueError("模组列表配置格式错误：不是有效的JSON对象")
                
            # 验证必要的字段
            for list_id, list_info in mod_lists.items():
                if not isinstance(list_info, dict):
                    raise ValueError(f"模组列表 {list_id} 配置格式错误")
                required_fields = ['name', 'file', 'description']
                for field in required_fields:
                    if field not in list_info:
                        raise ValueError(f"模组列表 {list_id} 缺少必要字段: {field}")
            
            # 更新缓存
            ModUtils._config_cache['mod_lists'] = mod_lists
            ModUtils._config_cache_time['mod_lists'] = current_time
            
            return mod_lists
        except Exception as e:
            logging.error(f"获取模组列表配置失败: {type(e).__name__} {e}")
            return {
                'default': {
                    'name': '默认模组列表',
                    'file': 'mod_info.json',
                    'description': '默认的模组列表'
                }
            }

    @staticmethod
    def check_path_permissions(path: str) -> Tuple[bool, str]:
        """检查路径的读写权限
        返回: (是否有权限, 错误信息)
        """
        try:
            # 检查路径是否存在
            if not os.path.exists(path):
                # 尝试创建目录
                try:
                    os.makedirs(path)
                except PermissionError:
                    return False, "没有权限创建目录"
                except Exception as e:
                    return False, f"创建目录失败: {str(e)}"
            
            # 检查读写权限
            test_file = os.path.join(path, "test_permission.tmp")
            try:
                # 测试写入权限
                with open(test_file, 'w') as f:
                    f.write("test")
                # 测试读取权限
                with open(test_file, 'r') as f:
                    f.read()
                # 测试删除权限
                os.remove(test_file)
                return True, ""
            except PermissionError:
                return False, "没有足够的文件访问权限"
            except Exception as e:
                return False, f"权限检查失败: {str(e)}"
        except Exception as e:
            return False, f"路径检查失败: {str(e)}"

    @staticmethod
    def get_local_mods(local_path: str) -> Dict[str, dict]:
        """扫描本地模组文件夹，返回模组信息字典"""
        mods = {}
        
        # 检查路径权限
        has_permission, error_msg = ModUtils.check_path_permissions(local_path)
        if not has_permission:
            raise Exception(f"路径权限错误: {error_msg}")
            
        if not os.path.exists(local_path):
            return mods

        for folder in os.listdir(local_path):
            folder_path = os.path.join(local_path, folder)
            if os.path.isdir(folder_path):
                server_data_path = os.path.join(folder_path, 'ServerData.json')
                if os.path.exists(server_data_path):
                    try:
                        with open(server_data_path, 'r', encoding='utf-8-sig') as f:
                            data = json.load(f)
                            mod_id = data.get('id', '')
                            version = data.get('revision', {}).get('version', '')
                            if mod_id:
                                mods[mod_id] = {
                                    'name': folder,
                                    'version': version,
                                    'path': folder_path
                                }
                    except PermissionError:
                        logging.error(f"无法读取文件: {server_data_path}")
                        continue
                    except Exception:
                        continue
        return mods

    @staticmethod
    def get_minio_config_from_web() -> dict:
        """从网站获取 MinIO 配置"""
        try:
            # 检查缓存
            cache_key = 'minio_config'
            if cache_key in ModUtils._config_cache:
                cache_time = ModUtils._config_cache_time.get(cache_key, 0)
                if time.time() - cache_time < ModUtils.CONFIG_SERVER['cache_time']:
                    return ModUtils._config_cache[cache_key]
            
            # 从网络获取配置
            url = f"{ModUtils.CONFIG_SERVER['base_url']}{ModUtils.CONFIG_SERVER['endpoints']['minio_config']}"
            logging.info(f"正在从配置服务器获取 MinIO 配置")
            
            # 添加时间戳和no-cdn参数避免 CDN 缓存
            timestamp = int(time.time())
            url = f"{url}?t={timestamp}&no-cdn=1"
            
            headers = {
                'User-Agent': 'ModSyncTool/2.0',
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'X-No-CDN': '1'  # 添加自定义头，请求不使用CDN
            }
            response = requests.get(url, timeout=ModUtils.CONFIG_SERVER['timeout'], headers=headers)
            response.raise_for_status()
            
            # 记录响应内容（不包含敏感信息）
            logging.debug("成功获取配置内容")
            
            config = response.json()
            
            # 验证配置格式
            if not isinstance(config, dict):
                raise ValueError("配置格式错误：不是有效的JSON对象")
            
            # 记录配置键（不包含敏感信息）
            logging.debug(f"配置中的键: {list(config.keys())}")
            
            # 验证全局设置
            settings = config.get('settings')
            if not settings:
                raise ValueError("全局设置不存在")
            if not isinstance(settings, dict):
                raise ValueError("全局设置格式错误：不是有效的JSON对象")
                
            # 验证全局设置的必要字段
            required_settings = ['bucket', 'timeout', 'retry_count', 'retry_delay', 'max_workers']
            missing_fields = [field for field in required_settings if field not in settings]
            if missing_fields:
                raise ValueError(f"全局设置缺少必要字段: {', '.join(missing_fields)}")
            
            # 验证节点配置
            node_count = 0
            for node_id, node_info in config.items():
                if node_id == 'settings':
                    continue
                    
                node_count += 1
                if not isinstance(node_info, dict):
                    raise ValueError(f"节点 {node_id} 配置格式错误：不是有效的JSON对象")
                    
                required_fields = ['endpoint', 'port', 'access_key', 'secret_key']
                missing_fields = [field for field in required_fields if field not in node_info]
                if missing_fields:
                    raise ValueError(f"节点 {node_id} 缺少必要字段: {', '.join(missing_fields)}")
            
            if node_count == 0:
                raise ValueError("配置中没有找到任何节点")
            
            # 更新缓存
            ModUtils._config_cache[cache_key] = config
            ModUtils._config_cache_time[cache_key] = time.time()
            
            return config
        except requests.RequestException as e:
            logging.error(f"获取 MinIO 配置失败: 网络请求错误")
            return None
        except ValueError as e:
            logging.error(f"获取 MinIO 配置失败: {str(e)}")
            return None
        except Exception as e:
            logging.error(f"获取 MinIO 配置失败: 未知错误")
            return None

    @staticmethod
    def get_available_nodes() -> list:
        """获取可用的节点列表"""
        try:
            config = ModUtils.get_minio_config_from_web()
            if not config:
                return []
            
            # 返回所有配置的节点（排除 settings）
            return [node_id for node_id in config.keys() if node_id != 'settings']
        except Exception as e:
            logging.error(f"获取可用节点失败: {str(e)}")
            return []

    @staticmethod
    def get_node_info(node_id: str) -> dict:
        """获取指定节点的配置信息"""
        try:
            if not node_id:
                return None
                
            config = ModUtils.get_minio_config_from_web()
            if not config:
                return None
                
            if node_id not in config or node_id == 'settings':
                return None
                
            node_info = config[node_id]
            if not isinstance(node_info, dict):
                return None
                
            return node_info
        except Exception as e:
            logging.error(f"获取节点信息失败: {str(e)}")
            return None

    @staticmethod
    def get_minio_client(node_id: str = None) -> Minio:
        """获取 MinIO 客户端"""
        try:
            # 如果没有指定节点，使用最佳节点
            if not node_id:
                node_id = ModUtils.get_best_node()
                if not node_id:
                    raise Exception("无法获取可用节点")
            
            # 获取节点配置
            node_info = ModUtils.get_node_info(node_id)
            if not node_info:
                raise Exception(f"无法获取节点 {node_id} 的配置")
            
            # 获取全局设置
            config = ModUtils.get_minio_config_from_web()
            if not config or 'settings' not in config:
                raise Exception("无法获取 MinIO 全局设置")
            
            # 创建 MinIO 客户端
            client = Minio(
                endpoint=f"{node_info['endpoint']}:{node_info['port']}",
                access_key=node_info['access_key'],
                secret_key=node_info['secret_key'],
                secure=node_info.get('secure', False),
                region=node_info.get('region', '')
            )
            
            return client
        except Exception as e:
            logging.error("创建 MinIO 客户端失败")
            raise

    @staticmethod
    def get_remote_mods(mod_list=None):
        """获取远程模组列表"""
        try:
            # 获取模组列表配置
            mod_lists = ModUtils.get_mod_lists()
            if not mod_lists:
                raise ValueError("获取模组列表失败")
                
            # 如果没有指定列表，使用第一个可用的列表
            if not mod_list:
                list_config = next(iter(mod_lists.values()))
                logging.info(f"使用默认模组列表: {list_config['name']}")
            else:
                # 获取指定的模组列表
                list_config = mod_lists.get(mod_list)
                if not list_config:
                    # 如果找不到指定的列表，使用第一个可用的列表
                    list_config = next(iter(mod_lists.values()))
                    logging.info(f"未找到指定的模组列表: {mod_list}，使用可用列表: {list_config['name']}")
            
            if not list_config:
                return {}
            
            # 优先通过HTTP从配置服务器获取mod_info.json
            try:
                base_url = ModUtils.CONFIG_SERVER['base_url']
                mod_info_url = urljoin(base_url + '/', list_config['file'])
                timestamp = int(time.time())
                mod_info_url = f"{mod_info_url}?t={timestamp}&no-cdn=1"
                headers = {
                    'User-Agent': 'ModSyncTool/2.0',
                    'Accept': 'application/json',
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'X-No-CDN': '1'
                }
                response = requests.get(mod_info_url, timeout=ModUtils.CONFIG_SERVER['timeout'], headers=headers)
                response.raise_for_status()
                mods = response.json()
                # 确保每个模组都有完整的文件名信息
                for mod_id, info in mods.items():
                    if 'filename' not in info:
                        info['filename'] = f"{info['name']}_{mod_id}.7z"
                    if 'path' not in info:
                        info['path'] = info['filename']
                return mods
            except Exception as e:
                logging.warning(f"HTTP获取mod_info.json失败，尝试MinIO: {str(e)}")
            
            # 获取 MinIO 配置
            minio_config = ModUtils.get_minio_config_from_web()
            if not minio_config:
                raise Exception("无法获取 MinIO 配置")
                
            # 获取全局设置
            settings = minio_config.get('settings')
            if not settings:
                raise Exception("无法获取 MinIO 全局设置")
                
            bucket = settings.get('bucket')
            if not bucket:
                raise Exception("MinIO 配置中缺少 bucket 设置")
            
            # 获取模组信息文件
            temp_file = 'mod_info.json'
            try:
                # 尝试从潍坊节点获取
                try:
                    client = ModUtils.get_minio_client('weifang')
                    client.fget_object(
                        bucket,
                        list_config['file'],
                        temp_file
                    )
                except S3Error:
                    # 如果潍坊节点失败，尝试上海节点
                    client = ModUtils.get_minio_client('shanghai')
                    client.fget_object(
                        bucket,
                        list_config['file'],
                        temp_file
                    )
                
                # 读取模组信息
                with open(temp_file, 'r', encoding='utf-8') as f:
                    mods = json.load(f)
                    # 确保每个模组都有完整的文件名信息
                    for mod_id, info in mods.items():
                        if 'filename' not in info:
                            info['filename'] = f"{info['name']}_{mod_id}.7z"
                        if 'path' not in info:
                            info['path'] = info['filename']
                    return mods
                    
            except Exception as e:
                logging.error(f"获取模组信息失败: {str(e)}")
                return {}
                
            finally:
                # 清理临时文件
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except:
                        pass
                        
        except Exception as e:
            logging.error(f"获取远程模组列表失败: {str(e)}")
            return {}

    @staticmethod
    def compare_versions(local_version: str, remote_version: str) -> str:
        """比较版本号并返回状态"""
        if not local_version or local_version == 'unknown':
            return '未安装'
        if not remote_version or remote_version == 'unknown':
            return '云端未知'
        if local_version == remote_version:
            return '已同步'
        return '需要更新'

    @staticmethod
    def compare_mods(local_mods: Dict[str, dict], remote_mods: Dict[str, dict]) -> Tuple[List[str], List[str], List[str]]:
        """比较本地和远程模组，返回需要更新、新增和删除的模组ID列表"""
        to_update = []
        to_add = []
        to_remove = []

        # 检查需要更新和新增的模组
        for mod_id, remote_info in remote_mods.items():
            if mod_id in local_mods:
                local_version = local_mods[mod_id]['version']
                remote_version = remote_info['version']
                if local_version != remote_version:
                    to_update.append(mod_id)
            else:
                to_add.append(mod_id)

        return to_update, to_add, to_remove

    @staticmethod
    def calculate_file_hash(file_path: str) -> str:
        """计算文件的SHA256哈希值"""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()

    @staticmethod
    def verify_file(file_path: str, expected_size: Optional[int] = None, is_7z: bool = False) -> Tuple[bool, str]:
        """验证文件完整性
        Args:
            file_path: 文件路径
            expected_size: 预期文件大小（字节）
            is_7z: 是否是7z文件
        Returns:
            Tuple[bool, str]: (是否有效, 错误信息)
        """
        try:
            if not os.path.exists(file_path):
                return False, "文件不存在"
                
            # 检查文件大小
            if expected_size is not None:
                actual_size = os.path.getsize(file_path)
                if actual_size != expected_size:
                    return False, f"文件大小不匹配: 预期 {expected_size} 字节, 实际 {actual_size} 字节"
            
            # 如果是7z文件，验证完整性
            if is_7z:
                try:
                    # 先尝试打开文件
                    with open(file_path, 'rb') as f:
                        # 检查文件头部标识
                        header = f.read(6)
                        if header != b'7z\xbc\xaf\x27\x1c':
                            return False, "不是有效的7z文件格式"
                            
                    # 尝试解压测试
                    with py7zr.SevenZipFile(file_path, mode='r') as z:
                        try:
                            if not z.test():
                                return False, "7z文件完整性验证失败"
                        except Exception as e:
                            return False, f"7z文件测试失败: {str(e)}"
                    
                except Exception as e:
                    return False, f"7z文件验证失败: {str(e)}"
                    
            return True, "验证成功"
            
        except Exception as e:
            return False, f"验证过程出错: {str(e)}"

    @staticmethod
    def handle_backup(folder_path: str, action: str = 'backup') -> str:
        """处理模组文件夹的备份和恢复
        Args:
            folder_path: 模组文件夹路径
            action: 操作类型 ('backup' 或 'restore')
        Returns:
            str: 备份文件夹路径（备份时）或操作结果（恢复时）
        """
        if action == 'backup':
            if not os.path.exists(folder_path):
                return ""
                
            backup_path = f"{folder_path}_backup_{int(time.time())}"
            try:
                shutil.copytree(folder_path, backup_path)
                return backup_path
            except Exception as e:
                logging.error(f"备份失败: {str(e)}")
                return ""
                
        elif action == 'restore':
            if not os.path.exists(folder_path):
                return "备份不存在"
                
            try:
                if os.path.exists(folder_path):
                    shutil.rmtree(folder_path)
                shutil.copytree(folder_path, folder_path.replace('_backup_', ''))
                return "恢复成功"
            except Exception as e:
                logging.error(f"恢复失败: {str(e)}")
                return f"恢复失败: {str(e)}"
                
        return "无效的操作类型"

    @staticmethod
    def check_node_latency(endpoint: str, port: int) -> tuple:
        """检查节点延迟
        Args:
            endpoint: 节点地址
            port: 节点端口
        Returns:
            tuple: (延迟时间(毫秒), 是否可用)
        """
        try:
            if not endpoint or not port:
                return float('inf'), False
                
            # 构建健康检查URL
            url = f"http://{endpoint}:{port}/minio/health/live"
            
            # 设置请求头
            headers = {
                'User-Agent': 'ModSyncTool/2.0',
                'Accept': 'application/json',
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache',
                'X-No-CDN': '1'  # 添加自定义头，请求不使用CDN
            }
            
            # 发送请求并计算延迟
            start_time = time.time()
            response = requests.get(
                url,
                timeout=2,  # 2秒超时
                headers=headers
            )
            end_time = time.time()
            
            # 计算延迟（毫秒）
            latency = (end_time - start_time) * 1000
            
            # 检查响应状态
            is_available = response.status_code == 200
            
            if is_available:
                logging.debug(f"节点 {endpoint}:{port} 延迟: {latency:.2f}ms")
            else:
                logging.debug(f"节点 {endpoint}:{port} 不可用，状态码: {response.status_code}")
                
            return latency, is_available
            
        except requests.exceptions.Timeout:
            logging.debug(f"节点 {endpoint}:{port} 连接超时")
            return float('inf'), False
        except requests.exceptions.ConnectionError:
            logging.debug(f"节点 {endpoint}:{port} 连接失败")
            return float('inf'), False
        except Exception as e:
            logging.debug(f"检查节点 {endpoint}:{port} 延迟失败: {str(e)}")
            return float('inf'), False

    @staticmethod
    def get_node_bandwidth(node_id: str) -> Tuple[float, float]:
        """获取节点带宽
        Args:
            node_id: 节点ID
        Returns:
            Tuple[float, float]: (下载带宽 Mbps, 上传带宽 Mbps)
        """
        try:
            node_info = ModUtils.get_node_info(node_id)
            if not node_info:
                return 0.0, 0.0
                
            client = ModUtils.get_minio_client(node_id)
            stats = client.get_bucket_stats()
            
            # 获取带宽信息
            download_rate = stats.get('download_rate', 0)  # bytes/s
            upload_rate = stats.get('upload_rate', 0)      # bytes/s
            
            # 转换为 Mbps
            download_mbps = (download_rate * 8) / (1024 * 1024)
            upload_mbps = (upload_rate * 8) / (1024 * 1024)
            
            return download_mbps, upload_mbps
            
        except Exception as e:
            ModUtils.logger.error(f"获取节点 {node_id} 带宽失败: {str(e)}")
            return 0.0, 0.0

    @staticmethod
    def get_node_load(node_id: str) -> float:
        """获取节点负载
        Args:
            node_id: 节点ID
        Returns:
            float: 节点负载值（0-1之间，1表示负载最高）
        """
        try:
            node_info = ModUtils.get_node_info(node_id)
            if not node_info:
                return float('inf')
                
            # 获取节点延迟
            latency, is_available = ModUtils.check_node_latency(
                node_info['endpoint'],
                node_info['port']
            )
            
            if not is_available:
                return float('inf')
                
            # 计算负载值
            # 只使用延迟作为负载指标
            latency_score = min(latency / 1000, 1)  # 将延迟转换为0-1的值
            
            # 记录详细信息
            ModUtils.logger.debug(f"节点 {node_id} 状态:")
            ModUtils.logger.debug(f"- 延迟: {latency:.2f}ms")
            ModUtils.logger.debug(f"- 总负载: {latency_score:.2f}")
            
            return latency_score
            
        except Exception as e:
            ModUtils.logger.error(f"获取节点 {node_id} 负载失败: {str(e)}")
            return float('inf')

    @staticmethod
    def get_node_status(node_id: str) -> dict:
        """获取节点详细状态
        Args:
            node_id: 节点ID
        Returns:
            dict: 节点状态信息
        """
        try:
            node_info = ModUtils.get_node_info(node_id)
            if not node_info:
                return None
                
            # 获取延迟
            latency, is_available = ModUtils.check_node_latency(
                node_info['endpoint'],
                node_info['port']
            )
            
            # 获取负载
            load = ModUtils.get_node_load(node_id)
            
            return {
                'node_id': node_id,
                'endpoint': node_info['endpoint'],
                'port': node_info['port'],
                'is_available': is_available,
                'latency': latency,
                'load': load
            }
            
        except Exception as e:
            ModUtils.logger.error(f"获取节点 {node_id} 状态失败: {str(e)}")
            return None

    @staticmethod
    def get_best_node() -> str:
        """获取最佳节点（考虑负载均衡）
        Returns:
            str: 最佳节点的ID，如果没有可用节点则返回 None
        """
        try:
            available_nodes = ModUtils.get_available_nodes()
            if not available_nodes:
                return None
                
            best_node = None
            best_load = float('inf')
            
            # 获取所有节点的负载
            node_loads = {}
            for node_id in available_nodes:
                load = ModUtils.get_node_load(node_id)
                node_loads[node_id] = load
                
                if load < best_load:
                    best_load = load
                    best_node = node_id
            
            # 记录节点负载情况
            ModUtils.logger.debug("节点负载情况:")
            for node_id, load in node_loads.items():
                ModUtils.logger.debug(f"节点 {node_id}: {load:.2f}")
            
            return best_node
            
        except Exception as e:
            ModUtils.logger.error(f"获取最佳节点失败: {str(e)}")
            return None

    @staticmethod
    def get_minio_settings() -> dict:
        """获取 MinIO 全局设置"""
        try:
            config = ModUtils.get_minio_config_from_web()
            if not config:
                return None
                
            settings = config.get('settings')
            if not settings or not isinstance(settings, dict):
                return None
                
            return settings
        except Exception as e:
            logging.error(f"获取 MinIO 全局设置失败: {str(e)}")
            return None

    @staticmethod
    def set_speed_limit(speed_mbps: float):
        """设置下载速度限制
        Args:
            speed_mbps: 速度限制（MB/s），0表示不限制
        """
        if speed_mbps <= 0:
            ModUtils.DOWNLOAD_SPEED_LIMIT = 0
        else:
            # 将MB/s转换为字节/秒
            ModUtils.DOWNLOAD_SPEED_LIMIT = int(speed_mbps * 1024 * 1024)
        ModUtils.logger.info(f"设置下载速度限制: {speed_mbps}MB/s")

    @staticmethod
    def _limit_speed(data_size: int):
        """限制下载速度
        Args:
            data_size: 数据大小（字节）
        """
        if ModUtils.DOWNLOAD_SPEED_LIMIT <= 0:
            return

        current_time = time.time()
        with ModUtils._speed_limit_lock:
            # 清理过期的速度记录
            ModUtils._speed_limit_buffer = [
                (t, size) for t, size in ModUtils._speed_limit_buffer
                if current_time - t < ModUtils.SPEED_LIMIT_WINDOW
            ]
            
            # 添加新的速度记录
            ModUtils._speed_limit_buffer.append((current_time, data_size))
            
            # 计算当前窗口内的总数据量
            total_size = sum(size for _, size in ModUtils._speed_limit_buffer)
            
            # 如果超过速度限制，计算需要等待的时间
            if total_size > ModUtils.DOWNLOAD_SPEED_LIMIT * ModUtils.SPEED_LIMIT_WINDOW:
                sleep_time = (total_size / ModUtils.DOWNLOAD_SPEED_LIMIT) - ModUtils.SPEED_LIMIT_WINDOW
                if sleep_time > 0:
                    time.sleep(sleep_time)

    @staticmethod
    def download_mod(mod_id: str, target_path: str, max_retries: int = 3, progress_callback=None, preferred_node: str = None, mod_list=None) -> bool:
        """从Minio下载单个模组，支持负载均衡和断点续传"""
        # 检查目标路径权限
        has_permission, error_msg = ModUtils.check_path_permissions(target_path)
        if not has_permission:
            raise Exception(f"目标路径权限错误: {error_msg}")
            
        remote_mods = ModUtils.get_remote_mods(mod_list)  # 传入模组列表参数
        if mod_id not in remote_mods:
            raise Exception(f"模组 {mod_id} 在远程服务器上不存在")
            
        mod_name = remote_mods[mod_id]['name']
        remote_filename = remote_mods[mod_id].get('filename', f"{mod_name}_{mod_id}.7z")
        remote_path = remote_mods[mod_id]['path']
        
        # 确保目标路径存在
        os.makedirs(target_path, exist_ok=True)
        
        temp_file = os.path.join(target_path, f"{remote_filename}.tmp")
        final_file = os.path.join(target_path, remote_filename)
        
        ModUtils.logger.info(f"开始下载文件: {remote_path}")
        ModUtils.logger.info(f"临时文件路径: {temp_file}")
        ModUtils.logger.info(f"最终文件路径: {final_file}")
        
        for attempt in range(max_retries):
            try:
                # 获取最佳节点（考虑负载均衡）
                if preferred_node:
                    # 检查首选节点是否可用
                    minio_config = ModUtils.get_minio_config_from_web()
                    if not minio_config:
                        raise Exception("无法获取 MinIO 配置")

                    if preferred_node in minio_config:
                        load = ModUtils.get_node_load(preferred_node)
                        if load < float('inf'):
                            node_name = preferred_node
                            client = ModUtils.get_minio_client(preferred_node)
                            ModUtils.logger.info(f"使用首选节点: {node_name}，负载: {load:.2f}")
                        else:
                            ModUtils.logger.warning(f"首选节点 {preferred_node} 不可用，切换到最佳节点")
                            node_name = ModUtils.get_best_node()
                            if not node_name:
                                raise Exception("无法获取可用节点")
                            client = ModUtils.get_minio_client(node_name)
                    else:
                        ModUtils.logger.warning(f"首选节点 {preferred_node} 不存在，切换到最佳节点")
                        node_name = ModUtils.get_best_node()
                        if not node_name:
                            raise Exception("无法获取可用节点")
                        client = ModUtils.get_minio_client(node_name)
                else:
                    # 使用最佳节点（考虑负载均衡）
                    node_name = ModUtils.get_best_node()
                    if not node_name:
                        raise Exception("无法获取可用节点")
                    client = ModUtils.get_minio_client(node_name)
                
                minio_config = ModUtils.get_minio_config_from_web()
                if not minio_config:
                    raise Exception("无法获取 MinIO 配置")

                settings = minio_config.get('settings')
                if not settings:
                    raise Exception("无法获取 MinIO 全局设置")
                    
                bucket = settings.get('bucket')
                if not bucket:
                    raise Exception("MinIO 配置中缺少 bucket 设置")

                stat = client.stat_object(bucket, remote_path)
                file_size = stat.size
                
                # 检查本地临时文件，准备断点续传
                local_size = 0
                if os.path.exists(temp_file):
                    local_size = os.path.getsize(temp_file)
                    if local_size > file_size:
                        os.remove(temp_file)
                        local_size = 0
                
                # 下载到临时文件
                start_time = time.time()
                
                class ProgressThread(threading.Thread):
                    def __init__(self):
                        super().__init__()
                        self.daemon = True
                        self._stop_event = threading.Event()
                        self._downloaded_size = local_size
                        
                    def stop(self):
                        self._stop_event.set()
                        
                    def run(self):
                        while not self._stop_event.is_set():
                            try:
                                if os.path.exists(temp_file):
                                    current_size = os.path.getsize(temp_file)
                                    if current_size > self._downloaded_size:
                                        elapsed = time.time() - start_time
                                        speed = (current_size - local_size) / elapsed if elapsed > 0 else 0
                                        remain = file_size - current_size
                                        eta = remain / speed if speed > 0 else 0
                                        progress = (current_size / file_size) * 100
                                        if progress_callback:
                                            progress_callback(progress, speed, eta)
                                        self._downloaded_size = current_size
                            except Exception as e:
                                ModUtils.logger.error(f"进度更新错误: {str(e)}")
                            time.sleep(0.5)
                
                progress_thread = ProgressThread()
                progress_thread.start()
                
                try:
                    # 使用get_object替代fget_object
                    response = client.get_object(
                        bucket,
                        remote_path
                    )
                    
                    # 写入文件
                    with open(temp_file, 'wb') as f:
                        for chunk in response.stream(32*1024):  # 32KB chunks
                            # 应用速度限制
                            ModUtils._limit_speed(len(chunk))
                            f.write(chunk)
                            
                finally:
                    progress_thread.stop()
                    progress_thread.join()
                    if 'response' in locals():
                        response.close()
                        response.release_conn()
                
                # 验证文件完整性（仅检查文件大小）
                is_valid, error_msg = ModUtils.verify_file(temp_file, expected_size=file_size, is_7z=False)
                if not is_valid:
                    raise Exception(f"文件验证失败: {error_msg}")
                
                # 重命名为最终文件
                try:
                    if os.path.exists(final_file):
                        os.remove(final_file)
                    os.rename(temp_file, final_file)
                except PermissionError:
                    raise Exception("没有权限重命名文件")
                
                ModUtils.logger.info(f"文件下载完成: {final_file}")
                return True
                
            except Exception as e:
                if str(e) == "同步被用户取消":
                    raise
                if attempt == max_retries - 1:
                    ModUtils.logger.error(f"下载失败: {str(e)}")
                    raise Exception(f"下载模组失败: {str(e)}")
                ModUtils.logger.warning(f"下载失败，正在重试 ({attempt + 1}/{max_retries}): {str(e)}")
                # 清理临时文件
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except:
                        pass
                time.sleep(minio_config['settings']['retry_delay'])
                continue
        return False

    @staticmethod
    def get_available_mod_lists() -> List[Dict[str, str]]:
        """获取可用的模组列表
        Returns:
            List[Dict[str, str]]: 模组列表信息列表
        """
        mod_lists = ModUtils.get_mod_lists()
        return [
            {
                'id': list_id,
                'name': config['name'],
                'description': config['description']
            }
            for list_id, config in mod_lists.items()
        ]

    @staticmethod
    def process_mods_parallel(local_path: str, to_update: List[str], to_add: List[str], local_mods: Dict[str, dict], remote_mods: Dict[str, dict], progress_callback: Optional[Callable] = None) -> Dict[str, str]:
        """并行处理模组更新和下载"""
        # 准备下载任务
        download_tasks = []
        
        # 处理需要更新的模组
        for mod_id in to_update:
            mod_name = local_mods[mod_id]['name']
            mod_path = os.path.join(local_path, f"{mod_name}_{mod_id}")
            download_tasks.append((mod_id, mod_name, mod_path))
            
        # 处理需要新增的模组
        for mod_id in to_add:
            mod_name = remote_mods[mod_id]['name']
            mod_path = os.path.join(local_path, f"{mod_name}_{mod_id}")
            os.makedirs(mod_path, exist_ok=True)
            download_tasks.append((mod_id, mod_name, mod_path))
            
        # 并发下载
        download_status = ModUtils.download_mods_parallel(
            [task[0] for task in download_tasks],
            local_path,
            progress_callback
        )
        
        # 处理下载结果
        results = {}
        for mod_id, mod_name, mod_path in download_tasks:
            status = download_status.get(mod_id, "未知")
            
            if status == "完成":
                try:
                    # 使用完整的文件名
                    remote_filename = remote_mods[mod_id].get('filename', f"{mod_name}_{mod_id}.7z")
                    archive_path = os.path.join(local_path, remote_filename)
                    logging.info(f"准备解压文件: {archive_path}")
                    
                    if not os.path.exists(archive_path):
                        # 尝试使用旧的文件名格式
                        old_archive_path = os.path.join(local_path, f"{mod_id}.7z")
                        if os.path.exists(old_archive_path):
                            archive_path = old_archive_path
                            logging.info(f"使用旧文件名格式: {archive_path}")
                        else:
                            raise Exception(f"模组文件不存在: {archive_path}")
                    
                    # 验证文件完整性
                    is_valid, error_msg = ModUtils.verify_file(archive_path, is_7z=True)
                    if not is_valid:
                        raise Exception(f"文件验证失败: {error_msg}")
                    
                    # 解压文件
                    if ModUtils.extract_mod(archive_path, mod_path):
                        # 清理下载的7z文件
                        try:
                            os.remove(archive_path)
                            logging.info(f"清理下载文件: {archive_path}")
                        except Exception as e:
                            logging.error(f"清理下载文件失败: {str(e)}")
                        results[mod_id] = "成功"
                    else:
                        results[mod_id] = "解压失败"
                except Exception as e:
                    results[mod_id] = f"处理失败: {str(e)}"
            else:
                results[mod_id] = status
                
        return results 

    @staticmethod
    def restore_from_backup(backup_path: str, target_path: str) -> bool:
        """从备份恢复模组文件夹
        Args:
            backup_path: 备份文件夹路径
            target_path: 目标文件夹路径
        Returns:
            bool: 是否恢复成功
        """
        try:
            result = ModUtils.handle_backup(backup_path, 'restore')
            return result == "恢复成功"
        except Exception as e:
            logging.error(f"恢复备份失败: {str(e)}")
            return False 

    @staticmethod
    def extract_mod(archive_path: str, target_path: str) -> bool:
        """解压模组文件
        Args:
            archive_path: 7z文件路径
            target_path: 解压目标路径
        Returns:
            bool: 是否解压成功
        """
        try:
            import py7zr
            
            # 创建临时目录进行解压
            temp_extract_path = f"{target_path}_temp_extract"
            if os.path.exists(temp_extract_path):
                shutil.rmtree(temp_extract_path)
            os.makedirs(temp_extract_path, exist_ok=True)
            
            # 先解压到临时目录
            with py7zr.SevenZipFile(archive_path, mode='r') as z:
                z.extractall(temp_extract_path)
            
            # 检查解压后的文件结构
            extracted_items = os.listdir(temp_extract_path)
            
            # 如果只有一个文件夹且该文件夹名与目标文件夹名相同
            if len(extracted_items) == 1 and os.path.isdir(os.path.join(temp_extract_path, extracted_items[0])):
                inner_folder = os.path.join(temp_extract_path, extracted_items[0])
                folder_name = extracted_items[0]
                
                # 如果解压出来的文件夹名与目标文件夹名相同，说明有嵌套
                if folder_name == os.path.basename(target_path):
                    # 确保目标文件夹存在并为空
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    os.makedirs(target_path, exist_ok=True)
                    
                    # 移动内部文件夹中的所有内容到目标路径
                    for item in os.listdir(inner_folder):
                        src_path = os.path.join(inner_folder, item)
                        dst_path = os.path.join(target_path, item)
                        if os.path.isdir(src_path):
                            if os.path.exists(dst_path):
                                shutil.rmtree(dst_path)
                            shutil.copytree(src_path, dst_path)
                        else:
                            shutil.copy2(src_path, dst_path)
                else:
                    # 目标文件夹不存在，直接重命名
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    shutil.move(inner_folder, target_path)
            else:
                # 有多个文件/文件夹，直接移动所有内容
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                shutil.move(temp_extract_path, target_path)
            
            # 清理临时目录
            if os.path.exists(temp_extract_path):
                shutil.rmtree(temp_extract_path)
                
            return True
        except Exception as e:
            logging.error(f"解压模组失败: {str(e)}")
            # 清理临时目录
            if os.path.exists(f"{target_path}_temp_extract"):
                try:
                    shutil.rmtree(f"{target_path}_temp_extract")
                except:
                    pass
            return False 

    @staticmethod
    def verify_mod_folder(mod_path: str) -> Tuple[bool, str]:
        """验证模组文件夹的完整性
        Args:
            mod_path: 模组文件夹路径
        Returns:
            Tuple[bool, str]: (是否完整, 错误信息)
        """
        try:
            if not os.path.exists(mod_path):
                return False, "模组文件夹不存在"
                
            # 检查必要文件
            required_files = ['ServerData.json', 'addon.gproj', 'resourceDatabase.rdb']
            for file in required_files:
                file_path = os.path.join(mod_path, file)
                if not os.path.exists(file_path):
                    return False, f"缺少必要文件: {file}"
                    
            # 读取ServerData.json获取版本号
            try:
                with open(os.path.join(mod_path, 'ServerData.json'), 'r', encoding='utf-8') as f:
                    server_data = json.load(f)
                mod_version = server_data.get('revision', {}).get('version', '')
                if not mod_version:
                    return False, "ServerData.json 中缺少版本信息"
            except Exception as e:
                return False, f"ServerData.json 格式错误: {str(e)}"
                
            # 检查pak文件
            pak_files = [f for f in os.listdir(mod_path) if f.startswith('data') and f.endswith('.pak')]
            if not pak_files:
                return False, "未找到任何pak文件"
                
            # 检查分卷完整性
            pak_files.sort()  # 按文件名排序
            
            # 检查第一个文件是否为data.pak
            if pak_files[0] != 'data.pak':
                return False, "缺少主文件: data.pak"
                
            # 检查分卷文件
            for i, pak_file in enumerate(pak_files[1:], 1):  # 从第二个文件开始检查
                expected_name = f"data{i:03d}.pak"
                if pak_file != expected_name:
                    return False, f"分卷文件命名错误: {pak_file}，应为 {expected_name}"
                    
                # 检查manifest文件
                manifest_file = f"{pak_file}_{mod_version}_manifest.json"
                manifest_path = os.path.join(mod_path, manifest_file)
                if not os.path.exists(manifest_path):
                    return False, f"缺少manifest文件: {manifest_file}"
                    
                # 验证manifest文件中的版本号
                try:
                    with open(manifest_path, 'r', encoding='utf-8') as f:
                        manifest_data = json.load(f)
                    manifest_version = manifest_data.get('version', '')
                    if manifest_version != mod_version:
                        return False, f"manifest文件版本不匹配: {manifest_file}"
                except Exception as e:
                    return False, f"manifest文件格式错误: {manifest_file} - {str(e)}"
                    
            # 检查主文件的manifest
            main_manifest = f"data.pak_{mod_version}_manifest.json"
            main_manifest_path = os.path.join(mod_path, main_manifest)
            if not os.path.exists(main_manifest_path):
                return False, f"缺少主文件manifest: {main_manifest}"
                
            return True, "验证通过"
            
        except Exception as e:
            return False, f"验证过程出错: {str(e)}" 