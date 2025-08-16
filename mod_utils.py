import os
import json
import shutil
import time
import threading
import queue
import concurrent.futures
from typing import Dict, List, Tuple, Optional, Callable
import logging
import py7zr
import requests
from urllib.parse import urljoin
import semver
import hashlib
import uuid
from botocore.client import Config
from botocore.exceptions import ClientError
import boto3
import random
from boto3.s3.transfer import S3Transfer, TransferConfig

class SensitiveFilter(logging.Filter):
    """日志过滤器，用于过滤敏感信息"""
    def __init__(self):
        super().__init__()
        # 需要过滤的敏感信息模式（捕获键与值，整体替换为 key=[REDACTED]）
        import re
        self.patterns = [
            (re.compile(r'(access_key)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), 'access_key'),
            (re.compile(r'(secret_key)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), 'secret_key'),
            (re.compile(r'(password)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), 'password'),
            (re.compile(r'(token)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), 'token'),
            (re.compile(r'(credential)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE), 'credential'),
        ]

    def filter(self, record):
        if isinstance(record.msg, str):
            msg = record.msg
            for regex, key in self.patterns:
                msg = regex.sub(f"{key}=[REDACTED]", msg)
            record.msg = msg
        return True

# 配置日志过滤器：同时作用于专用 logger 与根 logger，避免敏感信息外泄
logger = logging.getLogger("ModSyncTool")
logger.addFilter(SensitiveFilter())
logging.getLogger().addFilter(SensitiveFilter())

class ModUtils:
    # 版本信息
    VERSION = "3.1.0"  # 当前版本
    MIN_VERSION = "3.1.0"  # 最低支持版本
    VERSION_CHECK_URL = "https://www.vivi141.com/version.json"  # 版本检查URL
    VERSION_CACHE_FILE = ".version_cache.json"
    
    logger = logger
    # 配置服务器
    CONFIG_SERVER = {
        'base_url': 'https://www.vivi141.com/upload',
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

    # 统一 User-Agent
    USER_AGENT = f"ModSyncTool/{VERSION}"

    @staticmethod
    def build_headers(accept: Optional[str] = 'application/json', extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造统一 UA 的请求头
        Args:
            accept: Accept 头，默认 application/json；传 None 则不设置
            extra: 额外覆盖/追加的头
        Returns:
            dict: 请求头
        """
        headers = {
            'User-Agent': ModUtils.USER_AGENT,
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'X-No-CDN': '1'
        }
        if accept:
            headers['Accept'] = accept
        if extra:
            headers.update(extra)
        return headers

    # 配置缓存
    _config_cache = {}
    _config_cache_time = {}
    # R2 索引与解析缓存
    # 统一缓存：解析后的键与一次性列举缓存
    _r2_index_cache: Dict[Tuple[str, str], List[Tuple[str, int]]] = {}
    _resolved_keys: Dict[str, str] = {}
    _r2_index_data: Optional[dict] = None
    _r2_index_time: float = 0.0
    _index_lock = threading.Lock()
    OBJECTS_LIST_CACHE_FILE = ".r2_objects_cache.json"
    OBJECTS_LIST_CACHE_TTL_SEC = 300

    @staticmethod
    def build_target_mod_path(base_dir: str, mod_name: str, mod_id: str) -> str:
        """生成目标模组目录：若 name 已包含 _{mod_id} 后缀则不再重复追加。
        Args:
            base_dir: 根目录
            mod_name: 模组名称（可能已包含 _modid）
            mod_id: 模组ID
        Returns:
            目标目录完整路径
        """
        try:
            if isinstance(mod_name, str) and mod_name.lower().endswith(f"_{mod_id.lower()}"):
                final_name = mod_name
            else:
                final_name = f"{mod_name}_{mod_id}"
        except Exception:
            final_name = f"{mod_name}_{mod_id}"
        return os.path.join(base_dir, final_name)

    # 下载队列
    download_queue = queue.Queue()
    # 下载状态字典
    download_status = {}
    # 下载锁
    download_lock = threading.Lock()

    # 令牌桶限速（全局 + 每线程）
    DOWNLOAD_SPEED_LIMIT = 0  # 全局上限（字节/秒），0 表示不限制
    _global_bucket = {
        'capacity': 0,
        'tokens': 0.0,
        'last_refill': time.time(),
    }
    _per_thread_buckets: Dict[int, dict] = {}
    _bucket_lock = threading.Lock()

    @classmethod
    def initialize(cls):
        """初始化类，检查版本并输出到日志"""
        # 按用户要求，禁用任何版本网络请求，仅输出当前版本
        cls.logger.info(f"当前版本: {cls.VERSION}")

    @staticmethod
    def categorize_error_message(err: Exception) -> str:
        """将异常归类为更友好的错误信息"""
        try:
            if isinstance(err, requests.Timeout):
                return "网络超时"
            if isinstance(err, requests.ConnectionError):
                return "网络连接错误"
            if isinstance(err, ClientError):
                code = int(err.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0))
                if code == 404:
                    return "对象不存在"
                if code == 403:
                    return "访问被拒绝"
                if code == 429:
                    return "请求过多（限流）"
                if code >= 500:
                    return f"服务端错误({code})"
            msg = str(err).lower()
            if 'no space' in msg or 'not enough space' in msg or 'disk' in msg:
                return "磁盘空间不足"
            if 'permission' in msg or 'denied' in msg:
                return "权限不足"
            if 'checksum' in msg or 'integrity' in msg or '验证失败' in msg:
                return "文件校验失败"
        except Exception:
            pass
        return str(err)

    @staticmethod
    def check_version() -> Tuple[bool, str, str, str]:
        """检查版本更新
        Returns:
            Tuple[bool, str, str, str]: (是否需要更新, 当前版本, 最新版本, 更新说明)
        """
        # 按用户要求，彻底禁用版本检查
        return False, ModUtils.VERSION, ModUtils.VERSION, "版本检查已禁用"

    @staticmethod
    def get_version_info() -> Tuple[bool, str, str, str]:
        """获取版本信息并检查更新
        Returns:
            Tuple[bool, str, str, str]: (是否需要更新, 当前版本, 最新版本, 消息)
        """
        # 按用户要求，彻底禁用版本检查
        return False, ModUtils.VERSION, ModUtils.VERSION, "版本检查已禁用"

    @staticmethod
    def fetch_config(endpoint):
        """从配置服务器获取配置"""
        try:
            # 添加时间戳和no-cdn参数避免 CDN 缓存
            timestamp = int(time.time())
            url = f"{ModUtils.CONFIG_SERVER['base_url']}/{endpoint.lstrip('/')}?t={timestamp}&no-cdn=1"
            headers = ModUtils.build_headers('application/json')
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
            
            # 检查读写权限（使用唯一临时文件，避免并发冲突）
            test_file = os.path.join(path, f".perm_test_{uuid.uuid4().hex}.tmp")
            try:
                # 测试写入权限（独占创建）
                with open(test_file, 'xb') as f:
                    f.write(b"test")
                # 测试读取权限
                with open(test_file, 'rb') as f:
                    _ = f.read()
                # 测试删除权限
                os.remove(test_file)
                return True, ""
            except FileExistsError:
                # 罕见冲突时重试一次
                try:
                    os.remove(test_file)
                except Exception:
                    pass
                return ModUtils.check_path_permissions(path)
            except PermissionError:
                try:
                    if os.path.exists(test_file):
                        os.remove(test_file)
                except Exception:
                    pass
                return False, "没有足够的文件访问权限"
            except Exception as e:
                try:
                    if os.path.exists(test_file):
                        os.remove(test_file)
                except Exception:
                    pass
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
                                    'path': folder_path,
                                    'size': ModUtils.get_folder_size_safe(folder_path)
                                }
                    except PermissionError:
                        logging.error(f"无法读取文件: {server_data_path}")
                        continue
                    except Exception:
                        continue
        return mods

    @staticmethod
    def get_folder_size_safe(path: str) -> int:
        try:
            total = 0
            for root, dirs, files in os.walk(path):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        continue
            return total
        except Exception:
            return 0

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
            
            headers = ModUtils.build_headers('application/json')
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
            
            # 是否强制仅使用 R2 S3 节点
            settings = config.get('settings', {}) if isinstance(config, dict) else {}
            force_r2_only = bool(settings.get('force_r2_only', False))

            node_ids = []
            for node_id, node_info in config.items():
                if node_id == 'settings':
                    continue
                if force_r2_only:
                    try:
                        endpoint = str(node_info.get('endpoint', ''))
                        if 'r2.cloudflarestorage.com' not in endpoint:
                            continue
                    except Exception:
                        continue
                node_ids.append(node_id)
            return node_ids
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
    def get_minio_client(node_id: str = None) -> boto3.client:
        """获取 Cloudflare R2 客户端（使用 boto3 兼容 S3）"""
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
            
            # 规范化 endpoint
            endpoint_host = str(node_info['endpoint'])
            try:
                if endpoint_host.startswith('http://') or endpoint_host.startswith('https://'):
                    from urllib.parse import urlparse
                    parsed = urlparse(endpoint_host)
                    endpoint_host = parsed.netloc or parsed.path
                if '/' in endpoint_host:
                    endpoint_host = endpoint_host.split('/', 1)[0]
            except Exception:
                endpoint_host = endpoint_host.split('://', 1)[-1].split('/', 1)[0]

            # 创建 boto3 S3 客户端，兼容 Cloudflare R2
            settings = ModUtils.get_minio_settings() or {}
            timeout_val = int(settings.get('timeout', ModUtils.CONFIG_SERVER.get('timeout', 10)))
            max_conns = int(max(8, int(settings.get('max_workers', 4)) * 2))
            client = boto3.client(
                's3',
                endpoint_url=f"https://{endpoint_host}:{node_info['port']}" if node_info.get('secure', False) else f"http://{endpoint_host}:{node_info['port']}",
                aws_access_key_id=node_info['access_key'],
                aws_secret_access_key=node_info['secret_key'],
                region_name=node_info.get('region', 'auto'),
                config=Config(
                    signature_version='s3v4',
                    connect_timeout=timeout_val,
                    read_timeout=timeout_val,
                    max_pool_connections=max_conns,
                    retries={'max_attempts': 1, 'mode': 'standard'}
                )
            )
            
            return client
        except Exception as e:
            logging.error("创建 Cloudflare R2 客户端失败")
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
                headers = ModUtils.build_headers('application/json')
                response = requests.get(mod_info_url, timeout=ModUtils.CONFIG_SERVER['timeout'], headers=headers)
                response.raise_for_status()
                mods = response.json()
                # 确保每个模组都有完整的文件名信息
                for mod_id, info in mods.items():
                    if 'filename' not in info:
                        info['filename'] = f"{info['name']}_{mod_id}.7z"
                    if 'path' not in info:
                        info['path'] = info['filename']
                    if 'size' not in info:
                        info['size'] = 0
                return mods
            except Exception as e:
                logging.warning(f"HTTP获取mod_info.json失败，尝试R2直连获取: {str(e)}")
            
            # 获取 R2 配置
            minio_config = ModUtils.get_minio_config_from_web()
            if not minio_config:
                raise Exception("无法获取 R2 配置")
                
            # 获取全局设置
            settings = minio_config.get('settings')
            if not settings:
                raise Exception("无法获取 R2 全局设置")
                
            bucket = settings.get('bucket')
            if not bucket:
                raise Exception("R2 配置中缺少 bucket 设置")
            
            # 获取模组信息文件
            temp_file = 'mod_info.json'
            try:
                # 通过 boto3 直接获取对象
                client = ModUtils.get_minio_client()
                obj = client.get_object(Bucket=bucket, Key=list_config['file'])
                with open(temp_file, 'wb') as f:
                    f.write(obj['Body'].read())
                
                # 读取模组信息
                with open(temp_file, 'r', encoding='utf-8') as f:
                    mods = json.load(f)
                    # 确保每个模组都有完整的文件名信息
                    for mod_id, info in mods.items():
                        if 'filename' not in info:
                            info['filename'] = f"{info['name']}_{mod_id}.7z"
                        if 'path' not in info:
                            info['path'] = info['filename']
                        if 'size' not in info:
                            info['size'] = 0
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
            
            # 如果是7z文件，做轻量校验（避免对大文件进行二次全面解压测试导致卡住）
            if is_7z:
                try:
                    # 检查7z头
                    with open(file_path, 'rb') as f:
                        header = f.read(6)
                        if header != b'7z\xbc\xaf\x27\x1c':
                            return False, "不是有效的7z文件格式"
                    # 仅解析头部与目录，不做全量 test
                    try:
                        with py7zr.SevenZipFile(file_path, mode='r') as z:
                            _ = z.getnames()  # 触发元数据解析
                    except Exception as e:
                        return False, f"7z元数据解析失败: {str(e)}"
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
    def check_node_latency(endpoint: str, port: int, samples: int = 3, method: str = 'tcp', secure: bool = False, verify_ssl: Optional[bool] = None) -> tuple:
        """检查节点延迟（更稳健，多次采样取中位数）
        Args:
            endpoint: 节点地址
            port: 节点端口
            samples: 采样次数，默认3次
            method: 'tcp' 仅TCP连接耗时；'http' 访问健康检查接口
        Returns:
            tuple: (延迟时间(毫秒), 是否可用)
        """
        try:
            if not endpoint or not port:
                return float('inf'), False

            import socket
            # 规范化 endpoint，去掉 http/https 前缀，避免 TCP 直连失败
            try:
                if isinstance(endpoint, str) and (endpoint.startswith('http://') or endpoint.startswith('https://')):
                    from urllib.parse import urlparse
                    parsed = urlparse(endpoint)
                    endpoint = parsed.netloc or parsed.path
            except Exception:
                endpoint = endpoint.split('://', 1)[-1]
            import statistics

            latencies_ms = []

            if method == 'tcp':
                for _ in range(max(1, samples)):
                    try:
                        start = time.perf_counter()
                        with socket.create_connection((endpoint, int(port)), timeout=1.5):
                            pass
                        end = time.perf_counter()
                        latencies_ms.append((end - start) * 1000.0)
                    except (socket.timeout, OSError):
                        continue
            else:
                # HTTP 健康检查作为备选
                scheme = 'https' if secure else 'http'
                url = f"{scheme}://{endpoint}:{port}/minio/health/live"
                headers = ModUtils.build_headers('application/json', {'Connection': 'close'})
                for _ in range(max(1, samples)):
                    try:
                        start = time.perf_counter()
                        # 默认开启证书校验；允许调用方显式关闭
                        if verify_ssl is None:
                            verify = True
                        else:
                            verify = bool(verify_ssl)
                        resp = requests.get(url, timeout=1.5, headers=headers, verify=verify)
                        end = time.perf_counter()
                        if resp.status_code == 200:
                            latencies_ms.append((end - start) * 1000.0)
                    except requests.RequestException:
                        continue

            if not latencies_ms:
                # TCP失败时尝试一次HTTP作为最终回退
                if method == 'tcp':
                    return ModUtils.check_node_latency(endpoint, port, samples=samples, method='http')
                return float('inf'), False

            median_latency = statistics.median(latencies_ms)
            return median_latency, True
        except Exception as e:
            logging.debug(f"检查节点 {endpoint}:{port} 延迟失败: {str(e)}")
            return float('inf'), False

    @staticmethod
    def get_node_bandwidth(node_id: str) -> Tuple[float, float]:
        """获取节点带宽（占位实现）
        MinIO Python SDK 无通用 get_bucket_stats 接口，此处返回 0 以避免误用引发异常。
        """
        try:
            # 若未来接入自定义探测，可在此实现（例如下载小对象测算速率）
            node_info = ModUtils.get_node_info(node_id)
            if not node_info:
                return 0.0, 0.0
            return 0.0, 0.0
        except Exception:
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
            # 若存在 r2s3，优先返回（配合仅 R2 的场景）
            if 'r2s3' in available_nodes:
                return 'r2s3'

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
            ModUtils.DOWNLOAD_SPEED_LIMIT = int(speed_mbps * 1024 * 1024)
        # 重置桶
        with ModUtils._bucket_lock:
            cap = ModUtils.DOWNLOAD_SPEED_LIMIT
            ModUtils._global_bucket = {
                'capacity': cap,
                'tokens': float(cap),
                'last_refill': time.time(),
            }
            ModUtils._per_thread_buckets.clear()
        ModUtils.logger.info(f"设置下载速度限制: {speed_mbps}MB/s")

    @staticmethod
    def _limit_speed(data_size: int):
        """令牌桶限速：消耗全局与当前线程桶令牌，不足则按需睡眠。"""
        if ModUtils.DOWNLOAD_SPEED_LIMIT <= 0 or data_size <= 0:
            return
        tid = threading.get_ident()
        now = time.time()
        with ModUtils._bucket_lock:
            # 全局桶补充
            g = ModUtils._global_bucket
            cap = float(ModUtils.DOWNLOAD_SPEED_LIMIT)
            if cap <= 0:
                return
            elapsed = max(0.0, now - g.get('last_refill', now))
            g['tokens'] = min(cap, g.get('tokens', 0.0) + elapsed * cap)
            g['last_refill'] = now

            # 线程桶
            b = ModUtils._per_thread_buckets.get(tid)
            if not b:
                # 分配线程桶容量为全局的 1/N（N 近似设为 8），避免单线程抢占
                tcap = cap / 8.0
                b = {'capacity': tcap, 'tokens': tcap, 'last_refill': now}
                ModUtils._per_thread_buckets[tid] = b
            else:
                telapsed = max(0.0, now - b.get('last_refill', now))
                b['tokens'] = min(b['capacity'], b.get('tokens', 0.0) + telapsed * b['capacity'])
                b['last_refill'] = now

            need = float(data_size)
            # 尝试消耗
            g_avail = g['tokens']
            t_avail = b['tokens']
            take = min(need, g_avail, t_avail)
            g['tokens'] -= take
            b['tokens'] -= take
            remain = need - take

        if remain > 0:
            # 计算时间：按更紧的桶来估算等待
            g_rate = float(ModUtils.DOWNLOAD_SPEED_LIMIT)
            t_rate = float(ModUtils.DOWNLOAD_SPEED_LIMIT) / 8.0
            rate = min(g_rate, t_rate)
            if rate > 0:
                time.sleep(remain / rate)

    @staticmethod
    def download_mod(
        mod_id: str,
        target_path: str,
        max_retries: int = 3,
        progress_callback=None,
        preferred_node: str = None,
        mod_list=None,
        resolved_path: Optional[str] = None,
        expected_size_override: Optional[int] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> bool:
        """从 Cloudflare R2 下载单个模组，支持负载均衡和断点续传"""
        # 检查目标路径权限
        has_permission, error_msg = ModUtils.check_path_permissions(target_path)
        if not has_permission:
            raise Exception(f"目标路径权限错误: {error_msg}")
            
        remote_mods = ModUtils.get_remote_mods(mod_list)  # 传入模组列表参数
        if mod_id not in remote_mods:
            raise Exception(f"模组 {mod_id} 在远程服务器上不存在")
            
        mod_name = remote_mods[mod_id]['name']
        remote_filename = remote_mods[mod_id].get('filename', f"{mod_name}_{mod_id}.7z")
        remote_path = remote_mods[mod_id].get('path') or remote_filename
        # 若调用方提供已解析路径或缓存中已有，优先使用
        if resolved_path:
            remote_path = resolved_path
        elif mod_id in ModUtils._resolved_keys:
            remote_path = ModUtils._resolved_keys[mod_id]
        
        # 确保目标路径存在
        os.makedirs(target_path, exist_ok=True)
        
        temp_file = os.path.join(target_path, f"{remote_filename}.tmp")
        final_file = os.path.join(target_path, remote_filename)
        
        ModUtils.logger.info(f"开始下载文件: {remote_path}")
        ModUtils.logger.info(f"临时文件路径: {temp_file}")
        ModUtils.logger.info(f"最终文件路径: {final_file}")
        
        # 默认重试等待基值，后续使用指数退避+抖动
        default_retry_delay = ModUtils.CONFIG_SERVER.get('retry_delay', 1)
        base_delay = default_retry_delay
        max_delay = 15

        def _is_retryable_error(err: Exception) -> bool:
            # requests 异常
            if isinstance(err, requests.RequestException):
                # 超时、连接错误可重试
                return True
            # boto3 ClientError: 5xx/429 可重试
            if isinstance(err, ClientError):
                try:
                    code = int(err.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0))
                    return code >= 500 or code == 429
                except Exception:
                    return False
            # 常见的临时IO问题
            transient = (BrokenPipeError, ConnectionError, TimeoutError)
            return isinstance(err, transient)

        def _backoff_sleep(attempt_idx: int):
            # 指数退避 + 抖动
            delay = min(max_delay, base_delay * (2 ** attempt_idx))
            jitter = random.uniform(0, delay * 0.25)
            time.sleep(delay + jitter)

        for attempt in range(max_retries):
            try:
                # 获取最佳节点（考虑负载均衡）
                if preferred_node:
                    # 检查首选节点是否可用
                    minio_config = ModUtils.get_minio_config_from_web()
                    if not minio_config:
                        raise Exception("无法获取 R2 配置")

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
                    raise Exception("无法获取 R2 配置")

                settings = minio_config.get('settings')
                if not settings:
                    raise Exception("无法获取 R2 全局设置")
                    
                bucket = settings.get('bucket')
                if not bucket:
                    raise Exception("R2 配置中缺少 bucket 设置")

                # 覆盖重试等待时间
                try:
                    current_retry_delay = int(settings.get('retry_delay', default_retry_delay))
                except Exception:
                    current_retry_delay = default_retry_delay

                # 判断是否为 R2 HTTP 直连模式
                http_mode = False
                node_info = None
                try:
                    # 当 preferred_node 存在时优先用其信息，否则用自动选择的 node_name
                    node_key = preferred_node if preferred_node else node_name
                    node_info = ModUtils.get_node_info(node_key)
                except Exception:
                    node_info = None

                if node_info and (node_info.get('type') == 'r2_http' or 'public_url_base' in node_info):
                    http_mode = True

                # 若为 R2（HTTP 模式或 R2 S3 端点），为对象键自动套前缀（仅限 R2）
                is_r2_s3 = False
                try:
                    if not http_mode and node_info and isinstance(node_info.get('endpoint'), str):
                        is_r2_s3 = 'r2.cloudflarestorage.com' in node_info['endpoint']
                except Exception:
                    is_r2_s3 = False

                if http_mode or is_r2_s3:
                    settings_for_prefix = ModUtils.get_minio_settings() or {}
                    obj_prefix = str(settings_for_prefix.get('object_prefix', 'uploads/1/mods')).strip('/')
                    norm_path = str(remote_path).lstrip('/').replace('\\', '/')
                    if obj_prefix and not norm_path.startswith(obj_prefix + '/'):
                        norm_path = f"{obj_prefix}/{norm_path}"
                    remote_path = norm_path

                # 统一：通过候选键探测真实对象键与大小（不做全量列举/索引）
                def _resolve_key_and_size() -> Tuple[Optional[str], Optional[int], Optional[str]]:
                    info = remote_mods.get(mod_id, {})
                    filename = info.get('filename', remote_filename)
                    name = info.get('name', '')
                    version = info.get('version', '')
                    object_prefix = str((settings or {}).get('object_prefix', 'uploads/1/mods')).strip('/')
                    key_template = str((settings or {}).get('object_key_template', '')).strip()

                    # 组装候选键
                    candidates: List[str] = []
                    # 1) 传入/缓存
                    if resolved_path:
                        candidates.append(str(resolved_path).lstrip('/'))
                    elif mod_id in ModUtils._resolved_keys:
                        candidates.append(str(ModUtils._resolved_keys[mod_id]).lstrip('/'))
                    # 2) 显式 path
                    raw_path = str(info.get('path') or '').strip()
                    if raw_path:
                        candidates.append(raw_path.lstrip('/'))
                    # 3) 模板
                    if key_template:
                        try:
                            rendered = key_template.format(
                                object_prefix=object_prefix,
                                mod_id=mod_id,
                                filename=filename,
                                name=name,
                                version=version,
                            ).strip('/').replace('\\', '/')
                            if rendered:
                                candidates.append(rendered)
                        except Exception:
                            pass
                    # 4) 常规：prefix/filename 与 filename
                    norm_filename = str(filename).lstrip('/').replace('\\', '/')
                    if object_prefix:
                        candidates.append(f"{object_prefix}/{norm_filename}")
                    candidates.append(norm_filename)

                    # 去重保持顺序
                    seen = set()
                    ordered_candidates = []
                    for c in candidates:
                        if not c:
                            continue
                        c = c.replace('\\', '/').lstrip('/')
                        if c not in seen:
                            seen.add(c)
                            ordered_candidates.append(c)

                    # 探测
                    if http_mode and node_info:
                        base = node_info.get('public_url_base')
                        if not base:
                            return None, None, None
                        include_bucket = bool(node_info.get('bucket_in_path', True))
                        http_timeout = max(2, int(settings.get('timeout', ModUtils.CONFIG_SERVER['timeout'])))
                        headers = ModUtils.build_headers('*/*', {'Connection': 'close'})
                        for key in ordered_candidates:
                            try:
                                if include_bucket:
                                    test_url = f"{base.rstrip('/')}/{bucket}/{key}"
                                else:
                                    test_url = f"{base.rstrip('/')}/{key}"
                                head_resp = requests.head(test_url, timeout=http_timeout, headers=headers, allow_redirects=True)
                                if head_resp.status_code < 400 and head_resp.headers.get('Content-Length'):
                                    size_val = int(head_resp.headers['Content-Length'])
                                    return key, size_val, test_url
                                # 某些环境禁 HEAD：降级到 GET range(0-0) 探测
                                get_headers = dict(headers)
                                get_headers['Range'] = 'bytes=0-0'
                                get_resp = requests.get(test_url, timeout=http_timeout, headers=get_headers, stream=True, allow_redirects=True)
                                if get_resp.status_code in (200, 206):
                                    cr = get_resp.headers.get('Content-Range', '')
                                    # Content-Range: bytes 0-0/12345
                                    if '/' in cr:
                                        total = cr.split('/')[-1]
                                        if total.isdigit():
                                            return key, int(total), test_url
                            except Exception:
                                continue
                            finally:
                                try:
                                    head_resp.close()
                                except Exception:
                                    pass
                                try:
                                    get_resp.close()
                                except Exception:
                                    pass
                        return None, None, None
                    else:
                        if not client:
                            return None, None, None
                        for key in ordered_candidates:
                            try:
                                st = client.head_object(Bucket=bucket, Key=key)
                                return key, int(st.get('ContentLength', 0) or 0), None
                            except Exception:
                                continue
                        return None, None, None

                # 解析真实键与大小
                real_key = None
                file_size = int(expected_size_override) if expected_size_override is not None else None
                url = None
                rk, sz, u = _resolve_key_and_size()
                if rk:
                    real_key = rk
                    ModUtils._resolved_keys[mod_id] = real_key
                if sz and not file_size:
                    file_size = int(sz)
                if u:
                    url = u
                # 若未解析出键，报错
                if not real_key:
                    raise Exception("无法定位远端对象键，请确保 mod_info.json 提供 'path' 或在 settings 中配置 'object_key_template'")
                
                # 检查本地临时文件，准备断点续传
                local_size = 0
                if os.path.exists(temp_file):
                    local_size = os.path.getsize(temp_file)
                    if file_size is not None and local_size > file_size:
                        os.remove(temp_file)
                        local_size = 0
                
                # 下载到临时文件
                start_time = time.time()
                cancel_flag = {'cancelled': False}
                progress_thread = None
                
                try:
                    if not http_mode:
                        # 优先使用 S3 并发多段下载（最快）。若存在部分临时文件则重新下载以避免复杂续传问题。
                        if os.path.exists(temp_file):
                            try:
                                os.remove(temp_file)
                            except Exception:
                                pass
                        # 进度计算器
                        class _S3CB:
                            def __init__(self, total):
                                self.total = float(total or 0)
                                self.done = 0
                                self.last_t = time.time()
                                self.last_b = 0
                            def __call__(self, bytes_amount):
                                try:
                                    self.done += int(bytes_amount or 0)
                                    now = time.time()
                                    dt = max(1e-3, now - self.last_t)
                                    ds = max(0, self.done - self.last_b)
                                    # 使用回调内即时增量作为瞬时速度
                                    speed = ds / dt
                                    self.last_t = now
                                    self.last_b = self.done
                                    p = (self.done / self.total * 100.0) if self.total > 0 else 0.0
                                    if progress_callback:
                                        progress_callback(mod_id, p, speed, 0, self.done, self.total, 'download')
                                except Exception:
                                    pass
                        cfg = ModUtils.get_minio_settings() or {}
                        # 进行一次小样本测速以自适应参数
                        try:
                            sample = min(int(cfg.get('tune_sample_bytes', 8 * 1024 * 1024)), int(file_size or 8 * 1024 * 1024))
                            s3_bps = ModUtils._measure_s3_speed(client, bucket, real_key, sample, pause_event)
                        except Exception:
                            s3_bps = 0
                        concurrency, chunk_size = ModUtils._choose_parallel_params(s3_bps, cfg, mode='s3')
                        transfer = S3Transfer(client, TransferConfig(
                            multipart_threshold=16 * 1024 * 1024,
                            multipart_chunksize=chunk_size,
                            max_concurrency=concurrency,
                            use_threads=True,
                        ))
                        cb = _S3CB(file_size or 0)
                        transfer.download_file(bucket, real_key, temp_file, callback=cb)
                    else:
                        # HTTP 并发分段下载：若已有部分临时文件，则退回到顺序续传（保留断点续传能力）。
                        http_timeout = max(5, int(settings.get('timeout', ModUtils.CONFIG_SERVER['timeout'])))
                        if not url:
                            base = node_info.get('public_url_base')
                            include_bucket = bool(node_info.get('bucket_in_path', True))
                            if include_bucket:
                                url = f"{base.rstrip('/')}/{bucket}/{real_key}"
                            else:
                                url = f"{base.rstrip('/')}/{real_key}"
                        # 选择模式
                        if local_size > 0 and file_size and local_size < file_size:
                            # 顺序续传回退
                            # 在顺序续传模式下启动监控线程（并发模式预分配文件长度会导致误判，故仅此处启用）
                            class ProgressThread(threading.Thread):
                                def __init__(self):
                                    super().__init__()
                                    self.daemon = True
                                    self._stop_event = threading.Event()
                                    self._downloaded_size = local_size
                                    self._last_time = start_time
                                    self._last_size = local_size
                                def stop(self):
                                    self._stop_event.set()
                                def run(self):
                                    while not self._stop_event.is_set():
                                        try:
                                            if os.path.exists(temp_file):
                                                current_size = os.path.getsize(temp_file)
                                                if current_size >= self._downloaded_size:
                                                    now_ts = time.time()
                                                    delta_t = now_ts - self._last_time
                                                    delta_s = max(0, current_size - self._last_size)
                                                    speed = (delta_s / delta_t) if delta_t > 0 else 0
                                                    remain = max(0, (file_size or 0) - current_size)
                                                    eta = remain / speed if speed > 0 else 0
                                                    progress = ((current_size / file_size) * 100) if file_size else 0
                                                    if progress_callback:
                                                        try:
                                                            ret = progress_callback(mod_id, progress, speed, eta, current_size, file_size or 0, 'download')
                                                            if ret is False:
                                                                cancel_flag['cancelled'] = True
                                                                break
                                                        except Exception as e:
                                                            ModUtils.logger.error(f"进度回调错误: {str(e)}")
                                                    self._downloaded_size = current_size
                                                    self._last_time = now_ts
                                                    self._last_size = current_size
                                        except Exception as e:
                                            ModUtils.logger.error(f"进度更新错误: {str(e)}")
                                        time.sleep(0.3)
                            progress_thread = ProgressThread()
                            progress_thread.start()
                            import requests as _rq
                            from requests.adapters import HTTPAdapter
                            headers = ModUtils.build_headers('*/*', {'Connection': 'keep-alive'})
                            headers['Range'] = f'bytes={local_size}-'
                            _session = _rq.Session()
                            try:
                                adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
                                _session.mount('http://', adapter)
                                _session.mount('https://', adapter)
                            except Exception:
                                pass
                            _resp = None
                            try:
                                _resp = _session.get(url, timeout=http_timeout, headers=headers, stream=True, allow_redirects=True)
                                _resp.raise_for_status()
                                with open(temp_file, 'ab') as f:
                                    for chunk in _resp.iter_content(chunk_size=2*1024*1024):
                                        if pause_event is not None and not pause_event.is_set():
                                            while pause_event is not None and not pause_event.is_set():
                                                if cancel_flag['cancelled']:
                                                    raise Exception("同步被用户取消")
                                                time.sleep(0.1)
                                        if cancel_flag['cancelled']:
                                            raise Exception("同步被用户取消")
                                        if not chunk:
                                            continue
                                        ModUtils._limit_speed(len(chunk))
                                        f.write(chunk)
                            finally:
                                try:
                                    if _resp is not None:
                                        _resp.close()
                                except Exception:
                                    pass
                                try:
                                    _session.close()
                                except Exception:
                                    pass
                        else:
                            # 并发分段下载（自适应参数）
                            try:
                                sample = min(int(ModUtils.get_minio_settings().get('tune_sample_bytes', 4 * 1024 * 1024)), int(file_size or 4 * 1024 * 1024))
                            except Exception:
                                sample = min(4 * 1024 * 1024, int(file_size or 4 * 1024 * 1024))
                            try:
                                http_bps = ModUtils._measure_http_speed(url, sample, pause_event)
                            except Exception:
                                http_bps = 0
                            conc, part_sz = ModUtils._choose_parallel_params(http_bps, ModUtils.get_minio_settings() or {}, mode='http')
                            ModUtils._http_parallel_download(url, temp_file, int(file_size or 0), pause_event, progress_callback, mod_id, part_size=part_sz, concurrency=conc)
                finally:
                    try:
                        if progress_thread is not None:
                            progress_thread.stop()
                            progress_thread.join()
                    except Exception:
                        pass
                    try:
                        resp_local = locals().get('response')
                        if isinstance(resp_local, dict):
                            body = resp_local.get('Body')
                            try:
                                if hasattr(body, 'close'):
                                    body.close()
                            except Exception:
                                pass
                    except Exception:
                        pass
                
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
                # 仅标记内部状态，不对 UI 发100%完成信号，避免主线程误判任务已全部完成
                try:
                    if mod_id in ModUtils.download_status:
                        ModUtils.download_status[mod_id] = 'downloaded'
                except Exception:
                    pass
                return True
                
            except Exception as e:
                if str(e) == "同步被用户取消":
                    raise
                # 不可重试错误直接失败
                if not _is_retryable_error(e) or attempt == max_retries - 1:
                    ModUtils.logger.error(f"下载失败: {str(e)}")
                    raise Exception(f"下载模组失败: {str(e)}")
                ModUtils.logger.warning(f"下载失败，正在重试 ({attempt + 1}/{max_retries}): {str(e)}")
                # 清理临时文件
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except:
                        pass
                # 指数退避 + 抖动
                _backoff_sleep(attempt)
                continue
        return False

    @staticmethod
    def _http_parallel_download(url: str, temp_file: str, file_size: int, pause_event: Optional[threading.Event], progress_callback: Optional[Callable], mod_id: Optional[str], part_size: Optional[int] = None, concurrency: Optional[int] = None):
        import math
        import requests as _rq
        from requests.adapters import HTTPAdapter
        # 准备文件
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
        with open(temp_file, 'wb') as f:
            try:
                if file_size and file_size > 0:
                    f.truncate(file_size)
            except Exception:
                pass
        # 分段参数
        settings = ModUtils.get_minio_settings() or {}
        if part_size is None:
            part_size = int(settings.get('http_part_size', 4 * 1024 * 1024))
        if concurrency is None:
            concurrency = int(settings.get('http_parallelism', max(8, (os.cpu_count() or 2) * 2)))
        if not file_size or file_size <= 0:
            # 无法分段时降级为单线程
            part_size = 2 * 1024 * 1024
            concurrency = 1
        num_parts = math.ceil(file_size / part_size) if file_size > 0 else 1
        # 进度聚合
        progress = {
            'done': 0,
            'total': float(file_size or 0),
            'last_t': time.time(),
            'last_b': 0,
            'lock': threading.Lock(),
        }

        def report(add_bytes: int):
            with progress['lock']:
                progress['done'] += int(add_bytes or 0)
                now = time.time()
                dt = max(1e-3, now - progress['last_t'])
                ds = max(0, progress['done'] - progress['last_b'])
                speed = ds / dt
                progress['last_t'] = now
                progress['last_b'] = progress['done']
                p = (progress['done'] / progress['total'] * 100.0) if progress['total'] > 0 else 0.0
                if progress_callback:
                    try:
                        progress_callback(mod_id, p, speed, 0, progress['done'], progress['total'], 'download')
                    except Exception:
                        pass

        # 建立任务队列
        ranges = []
        for i in range(num_parts):
            start = i * part_size
            end = min(file_size - 1, (i + 1) * part_size - 1) if file_size > 0 else None
            ranges.append((start, end))

        def worker(tasks: queue.Queue):
            sess = _rq.Session()
            try:
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
                sess.mount('http://', adapter)
                sess.mount('https://', adapter)
            except Exception:
                pass
            headers_base = ModUtils.build_headers('*/*', {'Connection': 'keep-alive'})
            while True:
                if pause_event is not None and not pause_event.is_set():
                    while pause_event is not None and not pause_event.is_set():
                        time.sleep(0.1)
                try:
                    start, end = tasks.get_nowait()
                except Exception:
                    break
                try:
                    headers = dict(headers_base)
                    if end is not None:
                        headers['Range'] = f'bytes={start}-{end}'
                    resp = sess.get(url, headers=headers, stream=True, timeout=max(5, ModUtils.CONFIG_SERVER['timeout']), allow_redirects=True)
                    resp.raise_for_status()
                    with open(temp_file, 'r+b') as f:
                        f.seek(start)
                        for chunk in resp.iter_content(chunk_size=512*1024):
                            if not chunk:
                                continue
                            ModUtils._limit_speed(len(chunk))
                            f.write(chunk)
                            report(len(chunk))
                except Exception:
                    # 将失败的任务放回队列尾部，简易重试
                    try:
                        tasks.put((start, end))
                    except Exception:
                        pass
                    time.sleep(0.2)
                finally:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    tasks.task_done()
            try:
                sess.close()
            except Exception:
                pass

        q = queue.Queue()
        for it in ranges:
            q.put(it)
        threads = []
        for _ in range(min(concurrency, len(ranges))):
            t = threading.Thread(target=worker, args=(q,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    @staticmethod
    def _measure_s3_speed(client, bucket: str, key: str, sample_bytes: int, pause_event: Optional[threading.Event]) -> float:
        start = time.time()
        got = 0
        try:
            rng = f"bytes=0-{max(0, sample_bytes - 1)}"
            resp = client.get_object(Bucket=bucket, Key=key, Range=rng)
            body = resp['Body']
            for chunk in body.iter_chunks(chunk_size=512*1024):
                if pause_event is not None and not pause_event.is_set():
                    while pause_event is not None and not pause_event.is_set():
                        time.sleep(0.05)
                if not chunk:
                    continue
                got += len(chunk)
        finally:
            try:
                body.close()
            except Exception:
                pass
        dt = max(1e-3, time.time() - start)
        return got / dt

    @staticmethod
    def _measure_http_speed(url: str, sample_bytes: int, pause_event: Optional[threading.Event]) -> float:
        import requests as _rq
        from requests.adapters import HTTPAdapter
        sess = _rq.Session()
        try:
            adapter = HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
            sess.mount('http://', adapter)
            sess.mount('https://', adapter)
        except Exception:
            pass
        headers = ModUtils.build_headers('*/*', {'Range': f'bytes=0-{max(0, sample_bytes - 1)}', 'Connection': 'keep-alive'})
        start = time.time()
        got = 0
        resp = None
        try:
            resp = sess.get(url, headers=headers, stream=True, timeout=max(3, ModUtils.CONFIG_SERVER['timeout']), allow_redirects=True)
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=256*1024):
                if pause_event is not None and not pause_event.is_set():
                    while pause_event is not None and not pause_event.is_set():
                        time.sleep(0.05)
                if not chunk:
                    continue
                got += len(chunk)
        finally:
            try:
                if resp is not None:
                    resp.close()
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
        dt = max(1e-3, time.time() - start)
        return got / dt

    @staticmethod
    def _choose_parallel_params(speed_bps: float, settings: dict, mode: str = 's3') -> Tuple[int, int]:
        # 基于测得速率与 CPU 核数建议并发与分块；可被 settings 覆盖上限
        cpu = max(1, os.cpu_count() or 2)
        mbps = (speed_bps or 0) / (1024*1024)
        if mbps <= 2:
            concurrency = min(4, cpu)
            chunk = 1 * 1024 * 1024
        elif mbps <= 10:
            concurrency = min(8, cpu * 2)
            chunk = 2 * 1024 * 1024
        elif mbps <= 50:
            concurrency = min(12, cpu * 2)
            chunk = 4 * 1024 * 1024
        else:
            concurrency = min(16, cpu * 3)
            chunk = 8 * 1024 * 1024
        # 应用 settings 上限
        if mode == 's3':
            max_conc = int(settings.get('multipart_concurrency', concurrency))
            max_chunk = int(settings.get('multipart_chunksize', chunk))
        else:
            max_conc = int(settings.get('http_parallelism', concurrency))
            max_chunk = int(settings.get('http_part_size', chunk))
        concurrency = min(max_conc, max(1, concurrency))
        chunk = max(256*1024, min(max_chunk, chunk))
        return concurrency, chunk

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
    def prefetch_sizes(mod_ids: List[str], mod_list: Optional[str]) -> Dict[str, int]:
        """预取一组模组的对象大小（字节）。
        优先：一次性列举 S3 前缀（boto3 paginator），构建对象索引后批量匹配；
        回退：基于候选键逐个探测（S3 head_object 或 HTTP HEAD）。
        返回: { mod_id: size_bytes }
        """
        sizes: Dict[str, int] = {}
        try:
            remote_mods = ModUtils.get_remote_mods(mod_list)
            settings = ModUtils.get_minio_settings() or {}
            bucket = settings.get('bucket', '')
            # 选择节点
            node_name = ModUtils.get_best_node()
            node_info = ModUtils.get_node_info(node_name) if node_name else None
            http_mode = bool(node_info and (node_info.get('type') == 'r2_http' or 'public_url_base' in node_info))
            client = None
            if not http_mode and node_name:
                try:
                    client = ModUtils.get_minio_client(node_name)
                except Exception:
                    client = None
            else:
                # 即使处于 HTTP 模式，仍尝试获取任一可用 S3 节点用于列举
                try:
                    alt_node = ModUtils.get_best_node()
                    if alt_node:
                        client = ModUtils.get_minio_client(alt_node)
                except Exception:
                    client = None

            # 一次性列举：构建 key -> size 列表缓存
            obj_prefix = str(settings.get('object_prefix', 'uploads/1/mods')).strip('/')
            list_prefix = obj_prefix + '/' if obj_prefix else ''
            objects_index: List[Tuple[str, int]] = []
            cache_key = (bucket, list_prefix)
            # 磁盘缓存优先
            def _load_objects_cache() -> List[Tuple[str, int]]:
                try:
                    if not os.path.exists(ModUtils.OBJECTS_LIST_CACHE_FILE):
                        return []
                    with open(ModUtils.OBJECTS_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    rec = data.get(f"{bucket}|{list_prefix}")
                    if not rec:
                        return []
                    ts = float(rec.get('time', 0))
                    if time.time() - ts > ModUtils.OBJECTS_LIST_CACHE_TTL_SEC:
                        return []
                    entries = rec.get('entries') or []
                    out: List[Tuple[str, int]] = []
                    for it in entries:
                        k = str(it.get('key', ''))
                        if not k:
                            continue
                        s = int(it.get('size', 0) or 0)
                        out.append((k, s))
                    return out
                except Exception:
                    return []

            def _save_objects_cache(objs: List[Tuple[str, int]]):
                try:
                    record = {
                        'time': time.time(),
                        'entries': [{'key': k, 'size': s} for (k, s) in objs]
                    }
                    data = {}
                    if os.path.exists(ModUtils.OBJECTS_LIST_CACHE_FILE):
                        try:
                            with open(ModUtils.OBJECTS_LIST_CACHE_FILE, 'r', encoding='utf-8') as f:
                                data = json.load(f) or {}
                        except Exception:
                            data = {}
                    data[f"{bucket}|{list_prefix}"] = record
                    with open(ModUtils.OBJECTS_LIST_CACHE_FILE, 'w', encoding='utf-8') as f:
                        json.dump(data, f, ensure_ascii=False)
                except Exception:
                    pass

            # 内存缓存或磁盘缓存
            with ModUtils._index_lock:
                if cache_key in ModUtils._r2_index_cache and ModUtils._r2_index_cache[cache_key]:
                    objects_index = ModUtils._r2_index_cache[cache_key]
                else:
                    objects_index = _load_objects_cache()
                    if objects_index:
                        ModUtils._r2_index_cache[cache_key] = objects_index

            if client and bucket and not objects_index:
                try:
                    paginator = client.get_paginator('list_objects_v2')
                    for page in paginator.paginate(Bucket=bucket, Prefix=list_prefix):
                        for obj in page.get('Contents', []) or []:
                            k = str(obj.get('Key', ''))
                            if not k:
                                continue
                            s = int(obj.get('Size', 0) or 0)
                            objects_index.append((k, s))
                    with ModUtils._index_lock:
                        ModUtils._r2_index_cache[cache_key] = objects_index
                    _save_objects_cache(objects_index)
                except Exception:
                    # 列举失败则回退到逐项探测
                    objects_index = []
 
            for mid in mod_ids:
                try:
                    if mid not in remote_mods:
                        continue
                    info = remote_mods[mid]
                    filename = info.get('filename', f"{info.get('name','mod')}_{mid}.7z")
                    name = info.get('name', '')
                    version = info.get('version', '')
                    obj_prefix = str(settings.get('object_prefix', 'uploads/1/mods')).strip('/')
                    key_template = str(settings.get('object_key_template', '')).strip()

                    # 候选键
                    candidates: List[str] = []
                    # 已解析/缓存
                    if mid in ModUtils._resolved_keys:
                        candidates.append(str(ModUtils._resolved_keys[mid]).lstrip('/'))
                    raw_path = str(info.get('path') or '').strip()
                    if raw_path:
                        candidates.append(raw_path.lstrip('/'))
                    if key_template:
                        try:
                            rendered = key_template.format(
                                object_prefix=obj_prefix,
                                mod_id=mid,
                                filename=filename,
                                name=name,
                                version=version,
                            ).strip('/').replace('\\', '/')
                            if rendered:
                                candidates.append(rendered)
                        except Exception:
                            pass
                    norm_filename = str(filename).lstrip('/').replace('\\', '/')
                    if obj_prefix:
                        candidates.append(f"{obj_prefix}/{norm_filename}")
                    candidates.append(norm_filename)

                    # 去重
                    seen = set(); ordered = []
                    for c in candidates:
                        c = c.replace('\\', '/').lstrip('/')
                        if c and c not in seen:
                            seen.add(c); ordered.append(c)

                    # 优先用一次性列举的索引批量匹配（按后缀匹配，兼容随机前缀）
                    matched = False
                    if objects_index:
                        # 构造后缀集合：完整文件名、_mod_id.7z、filename
                        suffixes = []
                        suffixes.append('/' + norm_filename)
                        suffixes.append('_' + mid + '.7z')
                        suffixes.append(norm_filename)
                        for key, sz in objects_index:
                            for suf in suffixes:
                                if key.endswith(suf):
                                    sizes[mid] = int(sz)
                                    with ModUtils._index_lock:
                                        ModUtils._resolved_keys[mid] = key
                                    matched = True
                                    break
                            if matched:
                                break

                    # 若未匹配上，回退到逐项探测
                    if not matched:
                        if http_mode and node_info:
                            base = node_info.get('public_url_base')
                            include_bucket = bool(node_info.get('bucket_in_path', True))
                            for key in ordered:
                                try:
                                    url = f"{base.rstrip('/')}/{bucket}/{key}" if include_bucket else f"{base.rstrip('/')}/{key}"
                                    resp = requests.head(url, timeout=max(2, ModUtils.CONFIG_SERVER['timeout']), headers=ModUtils.build_headers('*/*'), allow_redirects=True)
                                    if resp.status_code < 400 and resp.headers.get('Content-Length'):
                                        sizes[mid] = int(resp.headers['Content-Length'])
                                        with ModUtils._index_lock:
                                            ModUtils._resolved_keys[mid] = key
                                        matched = True
                                        break
                                except Exception:
                                    continue
                        else:
                            if client:
                                for key in ordered:
                                    try:
                                        st = client.head_object(Bucket=bucket, Key=key)
                                        sizes[mid] = int(st.get('ContentLength', 0) or 0)
                                        with ModUtils._index_lock:
                                            ModUtils._resolved_keys[mid] = key
                                        matched = True
                                        break
                                    except Exception:
                                        continue
                except Exception:
                    continue
        except Exception:
            return sizes
        return sizes

    @staticmethod
    def download_mods_parallel(mod_ids: List[str], target_path: str, progress_callback: Optional[Callable] = None, preferred_node: Optional[str] = None, mod_list: Optional[str] = None, max_workers: Optional[int] = None, pause_event: Optional[threading.Event] = None) -> Dict[str, str]:
        """并行下载多个模组
        Args:
            mod_ids: 需要下载的模组ID列表
            target_path: 本地保存路径
            progress_callback: 进度回调（总体或分项）
            preferred_node: 指定节点
            mod_list: 指定模组列表
            max_workers: 线程数（默认取配置 settings.max_workers 或 4）
        Returns:
            dict: {mod_id: 状态(成功/失败原因)}
        """
        results: Dict[str, str] = {}
        try:
            settings = ModUtils.get_minio_settings() or {}
            if max_workers is None:
                suggested = max(1, (os.cpu_count() or 2) * 2)
                max_workers = min(int(settings.get('max_workers', 4)), suggested)

            # 第一步：一次性解析所有真实对象键与大小
            key_map: Dict[str, Tuple[str, int]] = {}
            try:
                # 复用预取尺寸逻辑，填充大小
                size_map = ModUtils.prefetch_sizes(mod_ids, mod_list)
                # 将已解析键缓存带入 key_map
                for mid in mod_ids:
                    resolved_key = ModUtils._resolved_keys.get(mid)
                    if resolved_key:
                        key_map[mid] = (resolved_key, int(size_map.get(mid, 0)))
                    else:
                        # 尝试临时解析
                        pass
            except Exception:
                pass

            def task(mod_id: str) -> Tuple[str, str]:
                try:
                    resolved_key, sz = key_map.get(mod_id, (None, None))
                    ok = ModUtils.download_mod(
                        mod_id,
                        target_path,
                        progress_callback=progress_callback,
                        preferred_node=preferred_node,
                        mod_list=mod_list,
                        resolved_path=resolved_key,
                        expected_size_override=sz if isinstance(sz, int) and sz > 0 else None,
                        pause_event=pause_event,
                    )
                    return mod_id, ("成功" if ok else "失败")
                except Exception as e:
                    return mod_id, f"失败: {ModUtils.categorize_error_message(e)}"

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {executor.submit(task, m): m for m in mod_ids}
                for future in concurrent.futures.as_completed(future_map):
                    mod_id, status = future.result()
                    results[mod_id] = status
        except Exception as e:
            ModUtils.logger.error(f"并行下载失败: {str(e)}")
        return results

    @staticmethod
    def process_mods_parallel(local_path: str, to_update: List[str], to_add: List[str], local_mods: Dict[str, dict], remote_mods: Dict[str, dict], progress_callback: Optional[Callable] = None, pause_event: Optional[threading.Event] = None, preferred_node: Optional[str] = None, mod_list: Optional[str] = None) -> Dict[str, str]:
        """并行处理：每个模组下载完成后立即解压，避免等待全部下载完成导致的延迟或取消时未解压。"""
        tasks: List[Tuple[str, str, str]] = []  # (mod_id, mod_name, mod_path)
        for mod_id in to_update:
            mod_name = local_mods[mod_id]['name']
            mod_path = ModUtils.build_target_mod_path(local_path, mod_name, mod_id)
            tasks.append((mod_id, mod_name, mod_path))
        for mod_id in to_add:
            mod_name = remote_mods[mod_id]['name']
            mod_path = ModUtils.build_target_mod_path(local_path, mod_name, mod_id)
            os.makedirs(mod_path, exist_ok=True)
            tasks.append((mod_id, mod_name, mod_path))

        results: Dict[str, str] = {}
        results_lock = threading.Lock()

        def do_one(mod_id: str, mod_name: str, mod_path: str) -> Tuple[str, str]:
            try:
                # 下载前再次确认根目录权限
                ok_perm_root, perr_root = ModUtils.check_path_permissions(local_path)
                if not ok_perm_root:
                    return mod_id, f"失败: 目标路径权限错误: {perr_root}"

                ok = ModUtils.download_mod(
                    mod_id,
                    local_path,
                    progress_callback=progress_callback,
                    preferred_node=preferred_node,
                    mod_list=mod_list,
                    pause_event=pause_event,
                )
                if not ok:
                    return mod_id, "下载失败"

                # 解压前检查目标父目录权限
                remote_filename = remote_mods[mod_id].get('filename', f"{mod_name}_{mod_id}.7z")
                archive_path = os.path.join(local_path, remote_filename)
                logging.info(f"准备解压文件: {archive_path}")
                parent_dir = os.path.dirname(mod_path) or local_path
                ok_perm, perr = ModUtils.check_path_permissions(parent_dir)
                if not ok_perm:
                    return mod_id, f"失败: 目标路径权限错误: {perr}"
                if not os.path.exists(archive_path):
                    # 等待文件系统落盘片刻
                    import time as _t
                    _t.sleep(0.1)
                if not os.path.exists(archive_path):
                    # 回退1：旧文件名
                    alt = os.path.join(local_path, f"{mod_id}.7z")
                    if os.path.exists(alt):
                        archive_path = alt
                        logging.info(f"使用旧文件名格式: {archive_path}")
                    else:
                        # 回退2：根据后缀匹配目录中符合 _{mod_id}.7z 的文件
                        try:
                            candidates = [f for f in os.listdir(local_path) if f.lower().endswith(f"_{mod_id.lower()}.7z")]
                        except Exception:
                            candidates = []
                        if candidates:
                            archive_path = os.path.join(local_path, candidates[0])
                            logging.info(f"使用匹配文件名: {archive_path}")
                        else:
                            return mod_id, "失败: 模组文件不存在"
                is_valid, error_msg = ModUtils.verify_file(archive_path, is_7z=True)
                if not is_valid:
                    return mod_id, f"失败: 文件验证失败: {error_msg}"
                # 确保目标目录可写
                ok_perm, perr = ModUtils.check_path_permissions(os.path.dirname(mod_path))
                if not ok_perm:
                    return mod_id, f"失败: 目标目录权限不足: {perr}"

                if ModUtils.extract_mod(archive_path, mod_path, progress_callback=progress_callback, mod_id=mod_id):
                    # 在删除前记录文件大小，用于发送最终完成信号
                    try:
                        final_size = os.path.getsize(archive_path)
                    except Exception:
                        final_size = None
                    # 发送完成信号：进度100%、速度0、ETA 0、已下载=总大小
                    try:
                        if progress_callback:
                            progress_callback(mod_id, 100.0, 0, 0, 1, 1, 'extract')
                    except Exception:
                        pass
                    # 清理下载的7z文件
                    try:
                        os.remove(archive_path)
                        logging.info(f"清理下载文件: {archive_path}")
                    except Exception as e:
                        logging.warning(f"清理下载文件失败: {str(e)}")
                    return mod_id, "成功"
                return mod_id, "解压失败"
            except Exception as e:
                return mod_id, f"失败: {ModUtils.categorize_error_message(e)}"

        settings = ModUtils.get_minio_settings() or {}
        suggested = max(1, (os.cpu_count() or 2) * 2)
        max_workers = min(int(settings.get('max_workers', 4)), suggested)
        # 采用线程池：任务完成后立刻回收、立刻执行后续解压逻辑（已在 do_one 内部完成）
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_map = {ex.submit(do_one, mid, name, path): mid for (mid, name, path) in tasks}
            for fut in concurrent.futures.as_completed(future_map):
                mid, st = fut.result()
                with results_lock:
                    results[mid] = st

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
    def extract_mod(archive_path: str, target_path: str, progress_callback: Optional[Callable] = None, mod_id: Optional[str] = None) -> bool:
        """解压模组文件
        Args:
            archive_path: 7z文件路径
            target_path: 解压目标路径
        Returns:
            bool: 是否解压成功
        """
        try:
            import py7zr

            logging.info(f"开始解压: {archive_path} -> {target_path}")

            # 创建临时目录进行解压
            temp_extract_path = f"{target_path}_temp_extract"
            if os.path.exists(temp_extract_path):
                shutil.rmtree(temp_extract_path)
            os.makedirs(temp_extract_path, exist_ok=True)

            # 流式解压：避免一次性占用过多内存，并监控进度
            with py7zr.SevenZipFile(archive_path, mode='r') as z:
                try:
                    names = z.getnames() or []
                except Exception:
                    names = []
                total_files = 0
                for n in names:
                    try:
                        if n and not str(n).endswith('/'):
                            total_files += 1
                    except Exception:
                        continue

                stop_flag = threading.Event()

                def _monitor_extract():
                    import time as _t
                    last_p = -1
                    while not stop_flag.is_set():
                        try:
                            done = 0
                            for root, _, files in os.walk(temp_extract_path):
                                done += len(files)
                            if total_files <= 0:
                                p = 0.0
                            else:
                                p = min(99.0, (done / max(1, total_files)) * 100.0)
                            if progress_callback and mod_id is not None and abs(p - last_p) >= 1.0:
                                try:
                                    progress_callback(mod_id, p, 0, 0, done, max(1, total_files), 'extract')
                                except Exception:
                                    pass
                                last_p = p
                        except Exception:
                            pass
                        _t.sleep(0.3)

                monitor_thread = None
                if progress_callback and mod_id is not None:
                    monitor_thread = threading.Thread(target=_monitor_extract, daemon=True)
                    monitor_thread.start()
                try:
                    z.extractall(temp_extract_path)
                finally:
                    stop_flag.set()
                    if monitor_thread:
                        monitor_thread.join(timeout=1.0)

            # 检查解压后的文件结构
            extracted_items = os.listdir(temp_extract_path)
            logging.info(f"解压完成，临时目录包含 {len(extracted_items)} 项: {extracted_items[:5]}{'...' if len(extracted_items)>5 else ''}")

            # 如果只有一个文件夹且该文件夹名与目标文件夹名相同
            if len(extracted_items) == 1 and os.path.isdir(os.path.join(temp_extract_path, extracted_items[0])):
                inner_folder = os.path.join(temp_extract_path, extracted_items[0])
                folder_name = extracted_items[0]

                # 如果解压出来的文件夹名与目标文件夹名相同，说明有嵌套
                if folder_name == os.path.basename(target_path):
                    logging.info("检测到与目标同名的嵌套目录，执行内容合并覆盖")
                    # 确保目标文件夹存在并为空
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    os.makedirs(target_path, exist_ok=True)

                    # 移动内部文件夹中的所有内容到目标路径（逐项复制，遇到同名先删再覆盖）
                    for item in os.listdir(inner_folder):
                        src_path = os.path.join(inner_folder, item)
                        dst_path = os.path.join(target_path, item)
                        if os.path.isdir(src_path):
                            if os.path.exists(dst_path):
                                shutil.rmtree(dst_path)
                            shutil.copytree(src_path, dst_path)
                        else:
                            # 覆盖写入
                            if os.path.exists(dst_path):
                                os.remove(dst_path)
                            shutil.copy2(src_path, dst_path)
                else:
                    logging.info("检测到单一根目录，与目标不同名，执行整体移动为目标目录")
                    # 目标文件夹不存在，直接重命名
                    if os.path.exists(target_path):
                        shutil.rmtree(target_path)
                    shutil.move(inner_folder, target_path)
            else:
                # 有多个文件/文件夹，直接移动所有内容
                logging.info("检测到多项内容，整体迁移到目标目录")
                if os.path.exists(target_path):
                    shutil.rmtree(target_path)
                shutil.move(temp_extract_path, target_path)

            # 清理临时目录
            if os.path.exists(temp_extract_path):
                shutil.rmtree(temp_extract_path)

            logging.info(f"解压完成，目标目录: {target_path}")
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