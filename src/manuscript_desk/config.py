"""
ScholarOne论文状态追踪系统配置文件

期刊配置和用户凭据已迁移到数据库管理（通过 /config 页面配置）。
此文件仅保留系统级配置和首次迁移用的默认数据。
"""

# 默认期刊配置（仅用于首次启动时迁移到数据库，之后通过 /config 页面管理）
JOURNALS = {
    "Management Science": {
        "url": "https://mc.manuscriptcentral.com/ms",
        "short_name": "MS",
        "description": "Management Science",
    },
    "Information Systems Research": {
        "url": "https://mc.manuscriptcentral.com/isr",
        "short_name": "ISR",
        "description": "Information Systems Research",
    },
    "INFORMS Journal on Computing": {
        "url": "https://mc.manuscriptcentral.com/ijoc",
        "short_name": "IJOC",
        "description": "INFORMS Journal on Computing",
    },
    "MIS Quarterly": {
        "url": "https://mc.manuscriptcentral.com/misq",
        "short_name": "MISQ",
        "description": "MIS Quarterly",
    },
}

# 默认用户配置（仅用于首次迁移，之后通过 /config 页面管理）
USER_CONFIG = {"username": "", "password": ""}

# 系统配置
SYSTEM_CONFIG = {
    "headless": False,
    "timeout": 15,
    "retry_count": 3,
    "wait_time": 2,
}

# Web界面配置
WEB_CONFIG = {
    "title": "ScholarOne论文状态追踪系统",
    "description": "自动追踪多个期刊的论文状态",
    "version": "2.0.0",
    "author": "ScholarOne Tracker",
}

# 日志配置
LOGGING_CONFIG = {
    "level": "INFO",
    "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    "file": "scholarone_tracker.log",
}
