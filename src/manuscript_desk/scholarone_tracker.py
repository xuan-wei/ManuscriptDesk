import logging
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from .browser_factory import create_chrome_driver
from .config import SYSTEM_CONFIG
from .provider_origins import (
    SafeProviderNavigation,
    UnsafeNavigationError,
    canonicalize_legacy_scholarone_url,
)
from .tracking_models import (
    ProviderLoginError,
    ProviderUnavailableError,
    raise_if_browser_tab_crashed,
)
from .tracking_policy import classify_status, extract_original_id_and_version

logger = logging.getLogger(__name__)

# 缓存已解析的 ChromeDriver 路径，避免同一 worker 重复走 webdriver-manager 的网络检查
_CACHED_DRIVER_PATH = None


def process_single_journal(task_data) -> dict:
    """处理单个期刊的函数 - 在独立进程中运行"""
    journal_name, journal_url, username, password, db_path = task_data

    # 为这个进程设置日志
    process_logger = logging.getLogger(f"process_{journal_name}")

    manuscripts = []
    manuscripts_found = 0
    error_message = ""
    success = False

    # 创建独立的 tracker 实例
    tracker = None
    try:
        process_logger.info(f"🔄 进程开始处理期刊: {journal_name}")

        # 创建新的 tracker 实例（headless模式以提高性能）
        tracker = ScholarOneTracker(headless=True, db_path=db_path)

        # 登录到期刊
        if tracker.login_to_journal(journal_url, username, password):
            # 导航到作者仪表板
            if tracker.navigate_to_author_dashboard():
                # 获取所有类型的论文状态

                # 1. 获取有决策的论文（我提交的）
                decisions_manuscripts = tracker.get_manuscripts_submitted_by_me()
                for manuscript in decisions_manuscripts:
                    manuscript.author_role = "primary"  # 我提交的论文
                manuscripts.extend(decisions_manuscripts)
                process_logger.info(f"{journal_name}: 获取到 {len(decisions_manuscripts)} 篇我提交的有决策论文")

                # 2. 获取共同作者论文（我合作的）
                coauthor_manuscripts = tracker.get_manuscripts_submitted_by_coauthors()
                for manuscript in coauthor_manuscripts:
                    manuscript.author_role = "coauthor"  # 我合作的论文
                manuscripts.extend(coauthor_manuscripts)
                process_logger.info(f"{journal_name}: 获取到 {len(coauthor_manuscripts)} 篇我合作的论文")

                # 设置期刊名称并保存到数据库
                for manuscript in manuscripts:
                    manuscript.journal = journal_name
                    tracker.save_manuscript_to_db(manuscript)

                manuscripts_found = len(manuscripts)
                process_logger.info(f"{journal_name}: 总共获取到 {manuscripts_found} 篇论文")
                success = True

                # 记录成功的查询日志
                tracker.db_manager.log_query(journal_name, True, manuscripts_found)
            else:
                error_message = "无法导航到作者仪表板"
                process_logger.error(f"无法导航到 {journal_name} 的作者仪表板")
                tracker.db_manager.log_query(journal_name, False, 0, error_message)
        else:
            error_message = "无法登录到期刊"
            process_logger.error(f"无法登录到 {journal_name}")
            tracker.db_manager.log_query(journal_name, False, 0, error_message)

    except Exception as e:
        error_message = f"期刊处理异常: {str(e)}"
        process_logger.error(f"处理期刊 {journal_name} 时出错: {str(e)}")
        if tracker:
            tracker.db_manager.log_query(journal_name, False, 0, error_message)

    finally:
        # 确保关闭浏览器
        if tracker:
            tracker.close()
            process_logger.info(f"🔚 {journal_name} 处理完成，浏览器已关闭")

    return {
        "journal_name": journal_name,
        "manuscripts": manuscripts,
        "success": success,
        "manuscripts_found": manuscripts_found,
        "error_message": error_message,
    }


@dataclass
class ManuscriptStatus:
    """论文状态数据类"""

    journal: str
    manuscript_id: str
    title: str
    submitted_date: str
    decision_date: str
    status_text: str  # 原始状态文本
    status: str  # 处理后的状态
    SE_or_DE: str  # 编辑信息（SE或DE）
    author_role: str = "primary"  # 'primary' = 我提交的论文, 'coauthor' = 我合作的论文
    last_updated: datetime = None


class ScholarOneTracker:
    """ScholarOne论文状态追踪器"""

    def _download_chrome_driver_arm64(self) -> str | None:
        """手动下载ARM64架构的ChromeDriver（支持Chrome >= 115的新API）"""
        try:
            import os
            import stat
            import tempfile
            import zipfile

            import requests

            chrome_version = self._get_chrome_version()
            if not chrome_version:
                return None

            major_version = chrome_version.split(".")[0]
            driver_dir = os.path.expanduser("~/.local/bin")
            os.makedirs(driver_dir, exist_ok=True)
            driver_path = os.path.join(driver_dir, "chromedriver")

            if int(major_version) >= 115:
                # Chrome >= 115: 使用 Chrome for Testing API
                api_url = f"https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_{major_version}"
                response = requests.get(api_url, timeout=10)
                if response.status_code != 200:
                    return None
                exact_version = response.text.strip()
                zip_url = f"https://storage.googleapis.com/chrome-for-testing-public/{exact_version}/mac-arm64/chromedriver-mac-arm64.zip"
            else:
                # Chrome < 115: 使用旧 API
                api_url = f"https://chromedriver.storage.googleapis.com/LATEST_RELEASE_{major_version}"
                response = requests.get(api_url, timeout=10)
                if response.status_code != 200:
                    return None
                exact_version = response.text.strip()
                zip_url = f"https://chromedriver.storage.googleapis.com/{exact_version}/chromedriver_mac_arm64.zip"

            logger.info(f"正在下载ChromeDriver {exact_version} for mac-arm64...")

            with tempfile.TemporaryDirectory() as temp_dir:
                zip_path = os.path.join(temp_dir, "chromedriver.zip")

                response = requests.get(zip_url, timeout=60)
                if response.status_code != 200:
                    logger.warning(f"下载ChromeDriver失败，HTTP状态码: {response.status_code}")
                    return None

                with open(zip_path, "wb") as f:
                    f.write(response.content)

                with zipfile.ZipFile(zip_path, "r") as zip_ref:
                    zip_ref.extractall(temp_dir)

                # Chrome for Testing 的 zip 结构: chromedriver-mac-arm64/chromedriver
                temp_driver = os.path.join(temp_dir, "chromedriver-mac-arm64", "chromedriver")
                if not os.path.exists(temp_driver):
                    temp_driver = os.path.join(temp_dir, "chromedriver")

                if os.path.exists(temp_driver):
                    if os.path.exists(driver_path):
                        os.remove(driver_path)
                    os.rename(temp_driver, driver_path)
                    os.chmod(driver_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
                    logger.info(f"成功下载ChromeDriver {exact_version} 到: {driver_path}")
                    return driver_path

            return None

        except Exception as e:
            logger.warning(f"手动下载ChromeDriver失败: {e}")
            return None

    def _get_chrome_version(self) -> str | None:
        """获取Chrome浏览器版本"""
        try:
            import platform
            import subprocess

            if platform.system() == "Darwin":  # macOS
                cmd = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--version"]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    version = result.stdout.strip().split()[-1]
                    return version

            return None
        except Exception as e:
            logger.warning(f"获取Chrome版本失败: {e}")
            return None

    def __init__(self, headless: bool = None, db_path: str | None = None):
        self.headless = headless if headless is not None else SYSTEM_CONFIG["headless"]
        self.driver = None
        self.db_manager = None
        if db_path:
            from database import DatabaseManager

            self.db_manager = DatabaseManager(db_path)
        self.setup_driver()

    def setup_driver(self):
        """设置Chrome浏览器驱动。"""
        self.driver = create_chrome_driver(headless=self.headless)
        self.driver.implicitly_wait(5)

    def login_to_journal(self, journal_url: str, username: str, password: str) -> bool:
        """登录到指定期刊的ScholarOne平台"""
        try:
            logger.info("正在打开 ScholarOne 登录页")
            navigation = SafeProviderNavigation(self.driver, "scholarone")
            navigation.open(canonicalize_legacy_scholarone_url(journal_url))

            # 查找用户名输入框
            username_field = WebDriverWait(self.driver, 5).until(EC.presence_of_element_located((By.NAME, "USERID")))
            navigation.before_credentials()
            username_field.clear()
            username_field.send_keys(username)

            # 查找密码输入框
            password_field = self.driver.find_element(By.NAME, "PASSWORD")
            password_field.clear()
            password_field.send_keys(password)

            # 点击登录按钮 (使用a标签的id)
            login_button = self.driver.find_element(By.ID, "logInButton")
            login_button.click()
            navigation.validate()

            # 等待登录完成，检查是否成功（检查是否有用户下拉菜单或Log Out链接）
            WebDriverWait(self.driver, SYSTEM_CONFIG["timeout"]).until(
                EC.any_of(
                    EC.presence_of_element_located((By.XPATH, "//a[contains(text(), 'Log Out')]")),
                    EC.presence_of_element_located((By.ID, "accountDropdownId")),
                    EC.presence_of_element_located((By.XPATH, "//a[contains(@onclick, 'LOGIN')]")),
                )
            )
            navigation.validate()

            logger.info("登录成功")
            return True

        except TimeoutException:
            logger.error("登录超时")
            return False
        except UnsafeNavigationError:
            raise
        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("登录失败: type=%s", type(exc).__name__)
            return False

    def navigate_to_author_dashboard(self):
        """导航到作者仪表板"""
        try:
            logger.info("🔍 开始导航到作者仪表板...")

            # 首先检查是否已经在作者页面
            current_url = self.driver.current_url
            page_source = self.driver.page_source
            logger.info(f"当前URL: {current_url}")

            # 如果页面已经包含作者相关的内容，直接返回成功
            author_keywords = [
                "Manuscripts with Decisions",
                "Submitted Manuscripts",
                "View Manuscripts",
                "Author Dashboard",
                "My Manuscripts",
                "Decisions",
            ]

            found_keywords = [keyword for keyword in author_keywords if keyword in page_source]
            if found_keywords:
                logger.info(f"✅ 已经在作者页面，找到关键词: {found_keywords}")
                # 检查是否有authorDashboardQueue表格（这是论文数据表格的ID）
                if "authorDashboardQueue" in page_source:
                    logger.info("✅ 检测到论文数据表格，Author页面已准备就绪")
                    return True

            try:
                if self.navigate_to_author_dashboard_try_href_based_links():
                    logger.info("✅ 导航到作者仪表板成功!")
                    SafeProviderNavigation(self.driver, "scholarone").validate()
                    return True
            except UnsafeNavigationError:
                raise
            except Exception as exc:
                raise_if_browser_tab_crashed(exc)
                logger.warning("导航到作者仪表板失败: type=%s", type(exc).__name__)

            return False

        except UnsafeNavigationError:
            raise
        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("导航到作者仪表板失败: type=%s", type(exc).__name__)
            return False

    def navigate_to_author_dashboard_try_href_based_links(self):
        """策略: 基于href属性查找链接"""
        href_patterns = [
            "//a[contains(@href, 'AUTHOR')]",
        ]

        for pattern in href_patterns:
            try:
                links = self.driver.find_elements(By.XPATH, pattern)
                logger.info(f"找到 {len(links)} 个匹配 '{pattern}' 的链接")

                for link in links:
                    try:
                        href = link.get_attribute("href")
                        text = link.text.strip()
                        logger.info(f"尝试href链接: {text} -> {href}")

                        self.driver.execute_script("arguments[0].click();", link)
                        SafeProviderNavigation(self.driver, "scholarone").validate()
                        time.sleep(1)  # 等待JavaScript执行和页面加载
                        if self._wait_for_author_dashboard(timeout=15):
                            SafeProviderNavigation(self.driver, "scholarone").validate()
                            return True
                    except UnsafeNavigationError:
                        raise
                    except Exception as exc:
                        raise_if_browser_tab_crashed(exc)
                        logger.warning("href链接点击失败: type=%s", type(exc).__name__)
                        continue
            except UnsafeNavigationError:
                raise
            except Exception as exc:
                raise_if_browser_tab_crashed(exc)
                logger.warning("href模式查找失败: type=%s", type(exc).__name__)
                continue
        return False

    def _wait_for_author_dashboard(self, timeout=10):
        """等待作者仪表板加载"""
        try:
            # 增加等待时间，使用更宽松的检测条件
            logger.info(f"⏳ 等待作者仪表板加载（{timeout}秒超时）...")

            # 等待页面加载完成
            WebDriverWait(self.driver, timeout).until(
                lambda driver: driver.execute_script("return document.readyState") == "complete"
            )

            # 检查多种可能的Author页面指标
            success_indicators = [
                # 检查页面内容中是否包含Author相关关键词
                lambda: "Manuscripts with Decisions" in self.driver.page_source,
                lambda: "Author Dashboard" in self.driver.page_source,
                lambda: "authorDashboardQueue" in self.driver.page_source,
                lambda: "My Manuscripts" in self.driver.page_source,
                # 检查特定元素是否存在
                lambda: len(self.driver.find_elements(By.XPATH, "//h1[contains(text(), 'Manuscripts with Decisions')]"))
                > 0,
                lambda: len(self.driver.find_elements(By.ID, "authorDashboardQueue")) > 0,
                lambda: len(self.driver.find_elements(By.XPATH, "//a[contains(text(), 'Manuscripts with Decisions')]"))
                > 0,
                lambda: len(
                    self.driver.find_elements(By.XPATH, "//div[contains(@class, 'nav')]//a[contains(text(), 'Author')]")
                )
                > 0,
            ]

            # 检查是否满足任何一个成功指标
            for i, indicator in enumerate(success_indicators, 1):
                try:
                    if indicator():
                        logger.info(f"✅ 成功检测到作者仪表板（指标 {i}）")
                        return True
                except Exception as exc:
                    raise_if_browser_tab_crashed(exc)
                    logger.debug("作者仪表板指标检测失败: type=%s", type(exc).__name__)
                    continue

            logger.warning("❌ 未检测到作者仪表板标识")
            return False

        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.warning("等待作者仪表板超时或出错: type=%s", type(exc).__name__)
            return False

    def _parse_manuscripts_table(self, submitted_by_me: bool) -> list[ManuscriptStatus]:
        """解析论文表格 - 使用data-label属性"""
        manuscripts = []

        try:
            # 查找authorDashboardQueue表格
            tables = self.driver.find_elements(By.ID, "authorDashboardQueue")
            if not tables:
                logger.warning("未找到authorDashboardQueue表格，无法解析")
                return []

            table = tables[0]
            logger.info("📊 找到表格")

            # 查找所有数据行
            rows = table.find_elements(By.CSS_SELECTOR, "tr[id^='queue_']")
            logger.info(f"📊 表格包含 {len(rows)} 行数据")

            for i, row in enumerate(rows, 1):
                try:
                    manuscript = self._parse_manuscript_row_simple(row, submitted_by_me)
                    if manuscript:
                        manuscripts.append(manuscript)
                        logger.info(f"✅ 第{i}行解析成功: {manuscript.manuscript_id} - {manuscript.title[:30]}...")
                    else:
                        logger.info(f"⏭️ 第{i}行跳过（可能是表头或空行）")
                except Exception as exc:
                    raise_if_browser_tab_crashed(exc)
                    logger.warning("解析论文行失败: type=%s", type(exc).__name__)
                    continue

            return manuscripts

        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("解析表格失败: type=%s", type(exc).__name__)
            return []

    def _parse_manuscript_row_simple(self, row, submitted_by_me: bool) -> ManuscriptStatus | None:
        """使用data-label属性解析论文行"""
        try:
            # 查找包含data-label属性的单元格
            cells = row.find_elements(By.CSS_SELECTOR, "td[data-label]")

            if len(cells) < 3:  # 至少需要ID、标题、状态
                logger.debug(f"跳过行：data-label单元格不足 ({len(cells)}个)")
                return None

            # 初始化数据字典，包含所有必需字段的默认值
            data = {
                "manuscript_id": "",
                "title": "",
                "status_text": "",
                "status": "",
                "SE_or_DE": "",
                "submitted_date": "",
                "decision_date": "",
            }

            # 根据data-label属性提取数据
            for cell in cells:
                label = cell.get_attribute("data-label").lower()

                if label == "id":
                    # 直接获取文本内容作为ID
                    data["manuscript_id"] = cell.text.strip()

                elif label == "title":
                    # 提取标题，忽略"View Submission"等链接
                    full_text = cell.text.strip()
                    # 分割文本，取第一行作为标题
                    title_lines = full_text.split("\n")
                    data["title"] = title_lines[0].strip() if title_lines else ""

                # 提取SE/DE信息
                if label == "status":
                    # 从复杂的状态单元格中提取决策信息
                    status_text = cell.text.strip().lstrip("Contact Journal").strip()
                    data["status_text"] = status_text

                    # 解析SE/DE行
                    try:
                        # 获取cell的HTML内容
                        cell_html = cell.get_attribute("innerHTML")
                        # 提取所有<nobr>标签内容
                        import re

                        nobr_matches = re.findall(r"<nobr>(.*?)</nobr>", cell_html, re.DOTALL)
                        se_or_de = None
                        for nobr in nobr_matches:
                            if nobr.strip().startswith("SE:") or nobr.strip().startswith("DE:"):
                                # 只取第一个SE/DE行
                                se_or_de = nobr.strip()
                                break
                        if se_or_de:
                            data["SE_or_DE"] = se_or_de.split(":")[1].strip()
                        else:
                            data["SE_or_DE"] = ""
                    except Exception as ex:
                        data["SE_or_DE"] = ""
                        logger.debug(f"提取SE/DE信息失败: {str(ex)}")

                    # 提取最后一个<tr>的<span class="pagecontents">内容作为status
                    try:
                        trs = cell.find_elements(By.CSS_SELECTOR, "table tr")
                        if trs:
                            # if '(' in one of trs, use that one; else use the last one
                            for tr in trs:
                                if "(" in tr.text.strip():
                                    data["status"] = tr.text.strip()
                                    break
                            else:
                                data["status"] = trs[-1].text.strip()
                        else:
                            data["status"] = status_text

                        status_with_decision_data = data["status"]
                        if "(" in status_with_decision_data:
                            data["status"] = status_with_decision_data.split("(")[0].strip()
                            data["decision_date"] = status_with_decision_data.split("(")[1].split(")")[0].strip()
                        else:
                            data["decision_date"] = ""
                    except Exception as ex:
                        data["status"] = status_text
                        data["decision_date"] = ""
                        logger.debug(f"提取status失败: {str(ex)}")

                elif label == "submitted":
                    data["submitted_date"] = cell.text.strip()

                elif label == "decisioned":
                    data["decision_date"] = cell.text.strip()

            # 检查是否有必需的数据
            manuscript_id = data.get("manuscript_id", "").strip()
            if not manuscript_id:
                logger.debug("跳过行：论文ID为空")
                return None

            title = data.get("title", "").strip()
            if not title:
                title = manuscript_id  # 如果没有标题，使用ID作为标题

            logger.debug(f"📝 解析结果: ID={manuscript_id}, 标题='{title[:30]}...', 决策='{data.get('decision', '')}'")

            return ManuscriptStatus(
                journal="",  # 将在调用处设置
                manuscript_id=manuscript_id,
                title=title,
                submitted_date=data.get("submitted_date", ""),
                decision_date=data.get("decision_date", ""),
                status_text=data.get("status_text", ""),
                status=data.get("status", ""),
                SE_or_DE=data.get("SE_or_DE", ""),
                last_updated=datetime.now(),
            )

        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("解析论文行失败: type=%s", type(exc).__name__)
            return None

    def get_manuscripts_submitted_by_me(self) -> list[ManuscriptStatus]:
        """获取我提交的论文列表"""
        manuscripts = []

        try:
            logger.info("🔍 开始查找论文信息...")

            # 首先检查数据是否已经在页面上显示
            page_source = self.driver.page_source
            if "authorDashboardQueue" in page_source and "Manuscripts with Decisions" in page_source:
                logger.info("✅ 检测到论文数据已经在页面上，直接解析表格")
                return self._parse_manuscripts_table(submitted_by_me=True)

            # 如果数据没有直接显示，尝试点击链接
            logger.info("📌 数据未直接显示，尝试查找并点击决策链接...")

            # 查找"Manuscripts with Decisions"链接
            decision_link_xpaths = [
                "//a[contains(text(), 'Manuscripts with Decisions')]",
                "//a[contains(text(), 'Decisions')]",
                "//li[contains(@class, 'nav-submenu')]//a[contains(text(), 'Manuscripts with Decisions')]",
            ]

            decisions_link = None
            for xpath in decision_link_xpaths:
                try:
                    links = self.driver.find_elements(By.XPATH, xpath)
                    if links:
                        logger.info(f"找到决策链接: {xpath}")
                        decisions_link = links[0]
                        break
                except Exception as exc:
                    raise_if_browser_tab_crashed(exc)
                    logger.warning("查找决策链接失败: type=%s", type(exc).__name__)
                    continue

            if decisions_link:
                # 点击找到的链接
                logger.info(f"📌 点击决策链接: {decisions_link.text}")
                self.driver.execute_script("arguments[0].click();", decisions_link)
                SafeProviderNavigation(self.driver, "scholarone").validate()

                # 解析表格
                manuscripts = self._parse_manuscripts_table(submitted_by_me=True)
            else:
                logger.info("❌ 未找到有决策论文链接，可能没有相关数据")

            logger.info(f"🎉 成功获取 {len(manuscripts)} 篇有决策的论文")
            return manuscripts

        except UnsafeNavigationError:
            raise
        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("获取论文状态失败: type=%s", type(exc).__name__)
            return []

    def get_manuscripts_submitted_by_coauthors(self) -> list[ManuscriptStatus]:
        """获取我作为合作者的论文列表（非主要作者）"""
        manuscripts = []

        try:
            logger.info("🔍 开始查找共同作者论文...")

            coauthor_link = None
            xpath = "//a[contains(@href, 'MS_LIST_TO_DISPLAY') and contains(@href, '5')]"
            try:
                links = self.driver.find_elements(By.XPATH, xpath)
                if links:
                    logger.info(f"找到共同作者链接: {xpath}")
                    coauthor_link = links[0]

            except Exception as exc:
                raise_if_browser_tab_crashed(exc)
                logger.warning("查找共同作者链接失败: type=%s", type(exc).__name__)

            if not coauthor_link:
                logger.info("❌ 未找到共同作者论文链接，可能没有共同作者论文")
                return []

            # 点击共同作者链接
            logger.info(f"📌 点击共同作者链接: {coauthor_link.text}")
            self.driver.execute_script("arguments[0].click();", coauthor_link)
            SafeProviderNavigation(self.driver, "scholarone").validate()

            # 解析共同作者论文表格
            manuscripts = self._parse_manuscripts_table(submitted_by_me=False)

            logger.info(f"🎉 成功获取 {len(manuscripts)} 篇共同作者论文")
            return manuscripts

        except UnsafeNavigationError:
            raise
        except Exception as exc:
            raise_if_browser_tab_crashed(exc)
            logger.error("获取共同作者论文失败: type=%s", type(exc).__name__)
            return []

    def save_manuscript_to_db(self, manuscript: ManuscriptStatus) -> bool:
        """将论文记录保存到数据库"""
        try:
            # 转换为字典格式
            manuscript_data = {
                "manuscript_id": manuscript.manuscript_id,
                "title": manuscript.title,
                "journal": manuscript.journal,
                "author_role": manuscript.author_role,
                "status_text": manuscript.status_text,
                "status": manuscript.status,
                "SE_or_DE": manuscript.SE_or_DE,
                "submitted_date": manuscript.submitted_date,
                "decision_date": manuscript.decision_date,
            }

            # 保存到统一记录表
            success = self.db_manager.save_manuscript_record(manuscript_data)
            if not success:
                logger.warning(f"保存论文记录失败: {manuscript.manuscript_id}")
                return False

            return True

        except Exception as e:
            logger.error(f"保存论文记录到数据库失败: {str(e)}")
            return False

    def get_all_manuscripts_status_from_configs(self, journal_configs: list) -> dict[str, list[ManuscriptStatus]]:
        """根据数据库中的期刊配置获取所有论文状态 - 每个期刊可以有独立的账号密码"""
        all_manuscripts = {}

        journal_tasks = [
            (cfg["journal_name"], cfg["journal_url"], cfg["username"], cfg["password"], self.db_manager.db_path)
            for cfg in journal_configs
        ]

        logger.info(f"开始并行处理 {len(journal_tasks)} 个期刊（从数据库配置），使用 4 个进程")

        try:
            with ProcessPoolExecutor(max_workers=4) as executor:
                future_to_journal = {executor.submit(process_single_journal, task): task[0] for task in journal_tasks}

                for future in as_completed(future_to_journal):
                    journal_name = future_to_journal[future]
                    try:
                        result = future.result()
                        all_manuscripts[journal_name] = result["manuscripts"]
                        logger.info(f"✅ {journal_name}: 处理完成，获取到 {len(result['manuscripts'])} 篇论文")
                    except Exception as exc:
                        logger.error(f"❌ {journal_name} 处理失败: {exc}")
                        all_manuscripts[journal_name] = []

        except Exception as e:
            logger.error(f"多进程处理期刊时出错: {str(e)}")
            logger.info("降级到单进程处理...")
            return self._get_all_manuscripts_status_sequential_from_configs(journal_configs)

        return all_manuscripts

    def _get_all_manuscripts_status_sequential_from_configs(
        self, journal_configs: list
    ) -> dict[str, list[ManuscriptStatus]]:
        """单进程顺序处理期刊配置（作为备用方案）"""
        all_manuscripts = {}

        if not self.driver:
            self.setup_driver()

        try:
            for cfg in journal_configs:
                journal_name = cfg["journal_name"]
                journal_url = cfg["journal_url"]
                username = cfg["username"]
                password = cfg["password"]

                logger.info(f"正在处理期刊: {journal_name}")
                manuscripts_found = 0
                error_message = ""

                try:
                    if self.login_to_journal(journal_url, username, password):
                        if self.navigate_to_author_dashboard():
                            manuscripts = []

                            decisions_manuscripts = self.get_manuscripts_submitted_by_me()
                            for manuscript in decisions_manuscripts:
                                manuscript.author_role = "primary"
                            manuscripts.extend(decisions_manuscripts)
                            logger.info(f"{journal_name}: 获取到 {len(decisions_manuscripts)} 篇我提交的有决策论文")

                            coauthor_manuscripts = self.get_manuscripts_submitted_by_coauthors()
                            for manuscript in coauthor_manuscripts:
                                manuscript.author_role = "coauthor"
                            manuscripts.extend(coauthor_manuscripts)
                            logger.info(f"{journal_name}: 获取到 {len(coauthor_manuscripts)} 篇我合作的论文")

                            for manuscript in manuscripts:
                                manuscript.journal = journal_name
                                self.save_manuscript_to_db(manuscript)

                            all_manuscripts[journal_name] = manuscripts
                            manuscripts_found = len(manuscripts)
                            logger.info(f"{journal_name}: 总共获取到 {manuscripts_found} 篇论文")
                            self.db_manager.log_query(journal_name, True, manuscripts_found)
                        else:
                            error_message = "无法导航到作者仪表板"
                            logger.error(f"无法导航到 {journal_name} 的作者仪表板")
                            all_manuscripts[journal_name] = []
                            self.db_manager.log_query(journal_name, False, 0, error_message)
                    else:
                        error_message = "无法登录到期刊"
                        logger.error(f"无法登录到 {journal_name}")
                        all_manuscripts[journal_name] = []
                        self.db_manager.log_query(journal_name, False, 0, error_message)

                except Exception as journal_error:
                    error_message = f"期刊处理异常: {str(journal_error)}"
                    logger.error(f"处理期刊 {journal_name} 时出错: {str(journal_error)}")
                    all_manuscripts[journal_name] = []
                    self.db_manager.log_query(journal_name, False, 0, error_message)

                time.sleep(1)

        except Exception as e:
            logger.error(f"获取所有论文状态时出错: {str(e)}")

        return all_manuscripts

    def fetch(self, config: dict, credentials) -> list[dict]:
        """Fetch one ScholarOne journal without writing to SQLite."""
        if not self.login_to_journal(config["journal_url"], credentials.username, credentials.password):
            raise ProviderLoginError()
        if not self.navigate_to_author_dashboard():
            raise ProviderUnavailableError()

        manuscripts = []
        decisions_manuscripts = self.get_manuscripts_submitted_by_me()
        for manuscript in decisions_manuscripts:
            manuscript.author_role = "primary"
        manuscripts.extend(decisions_manuscripts)

        coauthor_manuscripts = self.get_manuscripts_submitted_by_coauthors()
        for manuscript in coauthor_manuscripts:
            manuscript.author_role = "coauthor"
        manuscripts.extend(coauthor_manuscripts)

        snapshots = []
        for manuscript in manuscripts:
            source_original_id, version_number = extract_original_id_and_version(manuscript.manuscript_id, "scholarone")
            status, detail = classify_status(manuscript.status, "scholarone")
            snapshots.append(
                {
                    "provider": "scholarone",
                    "journal_config_id": config.get("id"),
                    "journal": config["journal_name"],
                    "manuscript_id": manuscript.manuscript_id,
                    "original_id": source_original_id,
                    "source_original_id": source_original_id,
                    "version_number": version_number,
                    "title": manuscript.title,
                    "author_role": manuscript.author_role,
                    "authorship": "",
                    "status_text": manuscript.status_text,
                    "status": status,
                    "detailed_status": detail,
                    "SE_or_DE": manuscript.SE_or_DE,
                    "submitted_date": manuscript.submitted_date,
                    "decision_date": manuscript.decision_date,
                    "status_date": "",
                    "final_disposition": "",
                    "workflow_folder": "",
                }
            )
        return snapshots

    def close(self):
        """关闭浏览器"""
        if self.driver:
            self.driver.quit()
            logger.info("浏览器已关闭")
