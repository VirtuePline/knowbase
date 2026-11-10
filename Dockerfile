# 工厂维保 / MES 运维知识库助手 — 生产镜像
#
# 构建：  docker build -t ag-kb:0.2.0 .
# 导出：  docker save -o ag-kb-docker.tar ag-kb:0.2.0
# 导入：  docker load -i ag-kb-docker.tar
# 运行：  docker compose up -d --no-build
#
# 镜像只打包代码、依赖和预先准备好的模型，不包含私有配置或业务文档。
# 原件、索引、图片分别持久化；模型保持在镜像内，不被空的数据挂载覆盖。
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    KB_HOST=0.0.0.0 \
    KB_ADMIN_HOST=0.0.0.0

# 先装依赖，充分利用 Docker 层缓存（国内网络用清华 PyPI 镜像，加大超时重试）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --timeout 120 --retries 5 -r requirements.txt

# 应用代码与前端页面
COPY app app
COPY web web

# 仅打包模型；构建前在本地准备好这两处模型文件以供离线部署。
COPY data/models/bge-reranker-base data/models/bge-reranker-base
COPY data/models/fastembed data/models/fastembed

# 私有覆盖由运行时只读挂载，不进入镜像层。
COPY config.yaml ./

EXPOSE 8000 8001
CMD ["python", "-m", "app.cli", "serve"]
