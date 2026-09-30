# Linux 离线包下载工具

在**有外网的 Windows**（或任意能联网的机器）上运行，从镜像源查询 RPM/DEB 软件包、**自动解析全部依赖**并批量下载，拷贝到**无外网的 Linux** 上安装。

## 特性

- 纯 Python 3 标准库实现，零第三方依赖（Python 3.8+）
- 支持 RHEL 系（CentOS 7 / CentOS Stream 9 / Rocky 9/10 / AlmaLinux 9/10 / Fedora 42，yum/dnf、.rpm）与 Debian 系（Ubuntu 22.04/24.04/25.04、Debian 12/13，apt、.deb）
- **扩展源一键勾选**：EPEL（7/9/10）、PostgreSQL 官方源 PGDG（14/15/16/17/18，含老系统 CentOS 7）。注意：清华不镜像 epel-archive/PGDG、中科大不镜像 PGDG，选这些镜像时扩展源自动改用阿里云/官方地址；单个仓库加载失败会跳过并继续加载其余仓库，不会再中断整个任务。
- **导入仓库 RPM**：粘贴第三方 repo rpm 链接（如 `https://download.postgresql.org/pub/repos/yum/reporpms/EL-7-x86_64/pgdg-redhat-repo-latest.noarch.rpm`），自动下载解析其中的 `.repo` 文件，按当前发行版替换 `$releasever`/`$basearch` 后把仓库地址追加到列表（支持 baseurl/mirrorlist/metalink；enabled=0 自动跳过）
- 镜像源预设（清华 / 阿里云 / 中科大 / 官方），可手填任意镜像地址
- 多仓库聚合解析依赖（BaseOS + AppStream / main + universe …）
- 依赖自动逐层解析，未满足依赖明确列出缺口清单
- 多线程并发下载（默认 6）、断点续传、失败自动重试、已存在自动跳过、SHA256 校验
- 下载完成自动生成本地仓库索引（RHEL: repodata/；Debian: Packages.gz），离线机可直接作为本地源
- 元数据本地缓存（cache/ 目录），二次启动秒开；勾选「强制刷新」可更新
- 默认仅监听 127.0.0.1，不对外暴露

## 启动

```
cd linux-offline-downloader
python server.py
```

浏览器自动打开 http://127.0.0.1:8080 。可选参数：`--port 9000` 换端口，`--host 0.0.0.0` 允许局域网访问（**不建议**，无任何鉴权），`--no-browser` 不自动开浏览器。

## 使用流程

1. **配置下载源**：选发行版体系 → 发行版 → 镜像源 → 目标架构，仓库列表自动生成；需要更多/更新的软件时勾选「扩展源」（EPEL、PGDG 等）后点「加载元数据」。
2. **查询**：输入包名搜索（自动忽略 `-_.` 分隔符，`postgresql14` 可命中 `postgresql-14`；也支持正则；包名无匹配时自动按描述兜底）。找不到包时：勾选扩展源、或勾选「强制刷新」更新缓存后重试。
3. **解析依赖**：查看完整依赖清单、总大小与未满足缺口。
4. **下载**：填本机保存目录（需已存在）或用「浏览…」选择，点开始下载。实时日志与进度在右侧任务区。

## 离线 Linux 端安装

把下载目录（如 `offline-packages`）整体拷贝到目标机后：

**RHEL 系（yum / dnf）**

```bash
# 方式一：直接安装全部 rpm
sudo yum install ./offline-packages/*.rpm

# 方式二：配为本地源（推荐，可继续使用 yum 装别的包）
cat | sudo tee /etc/yum.repos.d/offline.repo <<EOF
[offline]
name=offline
baseurl=file:///路径/offline-packages
enabled=1
gpgcheck=0
EOF
sudo yum --disablerepo='*' --enablerepo=offline install <包名>
```

**Debian 系（apt）**

```bash
# 方式一：直接安装全部 deb
sudo apt install ./offline-packages/*.deb

# 方式二：配为本地源
echo "deb [trusted=yes] file:///路径/offline-packages ./" | sudo tee /etc/apt/sources.list.d/offline.list
sudo apt update && sudo apt install <包名>
```

## 已知边界

- **CentOS 7 已停更**：其主源数据冻结于 2020 年（vault 存档），新版软件（如 postgresql14+）请勾选 PGDG/EPEL 扩展源获取。
- 元数据本地缓存永不过期，加载时日志会显示缓存时间；要拉取最新数据请勾选「强制刷新」。页头会显示缓存占用，点第 1 步的「清理缓存」可一键删除全部元数据缓存释放磁盘（不影响已下载的包文件）。
- 依赖解析为**尽力而为**：RPM 的虚拟提供（Provides）、epoch、版本范围在极端情况下可能与真实 yum/dnf 解析有出入；未满足的依赖会明确列出，可手动补下。
- 个别镜像的仓库元数据若使用 zstd 压缩（.zst），标准库无法解压，请换用 gzip 格式的镜像（清华/阿里云主流仓库均为 gzip）。
- 只下载 `x86_64/aarch64`（或 `amd64/arm64`）+ `noarch/all` 架构的包；32 位包不在范围内。
- 元数据缓存在 `cache/`，删除后可强制全量刷新。
