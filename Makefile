# Image configuration
IMAGE_REGISTRY ?= uhub.service.ucloud.cn/xiong
IMAGE_NAME ?= chat2k8s
IMAGE_TAG ?= $(shell date +%Y%m%d%H%M%S)
FULL_IMAGE_NAME = $(IMAGE_REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)

.PHONY: init run docinit build push

# 1: Project Initialization
# - Install python dependencies with uv
init:
	@echo "Installing Python dependencies..."
	uv sync

# 2: Run Main Application
run:
	@echo "Starting Chat2K8s..."
	uv run main.py

# Initialize/Overwrite Vector Database
docinit:
	@echo "Initializing Vector Database..."
	uv run scripts/rag-init.py

# 3: Build Docker Image
# - Sync git submodules
# - Build image with registry prefix
build:
	@echo "Syncing and updating submodules..."
	git submodule sync
	git submodule update --init --recursive
	@echo "Building docker image for linux/amd64: $(FULL_IMAGE_NAME)..."
	docker build --no-cache --platform linux/amd64 -t $(FULL_IMAGE_NAME) .
	@echo "Image build successful: $(FULL_IMAGE_NAME)"

# 4: Push Docker Image
push:
	@echo "Pushing docker image: $(FULL_IMAGE_NAME)..."
	docker push $(FULL_IMAGE_NAME)
	@echo "Image push successful: $(FULL_IMAGE_NAME)"

# 5: Run Docker Image locally
# - Mount .env and kubeConfig for local container testing
# - Set ENVIRONMENT=k8s to use internal MCP binaries
# - Mount guide_doc specifically for Filesystem MCP
docker-run:
	@echo "Running docker image locally: $(FULL_IMAGE_NAME)..."
	docker run -it --rm \
		--env-file .env \
		-e ENVIRONMENT=k8s \
		-v $(shell pwd)/kubeConfig:/app/kubeConfig \
		-v $(shell pwd)/doc_path/guide_doc:/projects \
		--name $(IMAGE_NAME) \
		$(FULL_IMAGE_NAME)
