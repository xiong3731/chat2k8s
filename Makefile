.PHONY: init build start stop status run clean

# Variables
DIST_DIR := dist
BINARY_NAME := kubernetes-mcp-server
MCP_SERVER_SRC := kubernetes-mcp-server
MCP_SERVER_CMD := ./cmd/kubernetes-mcp-server
PID_FILE := $(DIST_DIR)/mcp.pid
LOG_FILE := $(DIST_DIR)/mcp.log

# 1: Project Initialization
# - Install python dependencies with uv
# - Build kubernetes-mcp-server to dist directory
init:
	@echo "Updating submodules..."
	git submodule update --remote --init
	@echo "Installing Python dependencies..."
	uv sync
	@echo "Building kubernetes-mcp-server..."
	mkdir -p $(DIST_DIR)
	go build -C $(MCP_SERVER_SRC) -o $(CURDIR)/$(DIST_DIR)/$(BINARY_NAME) $(MCP_SERVER_CMD)

# 2: Start MCP Server (SSE Process)
# Runs in background, writes PID to file, logs to mcp.log
start:
	@if [ -f $(PID_FILE) ]; then \
		echo "MCP server is already running (PID: $$(cat $(PID_FILE)))"; \
	else \
		echo "Starting MCP server..."; \
		nohup ./$(DIST_DIR)/$(BINARY_NAME) \
			--kubeconfig kubeConfig/config.yaml \
			--read-only \
			--port 5678 \
			> $(LOG_FILE) 2>&1 & \
		echo $$! > $(PID_FILE); \
		echo "MCP server started with PID $$(cat $(PID_FILE)). Logs: $(LOG_FILE)"; \
	fi

# Stop MCP Server
stop:
	@if [ -f $(PID_FILE) ]; then \
		echo "Stopping MCP server (PID: $$(cat $(PID_FILE)))..."; \
		kill $$(cat $(PID_FILE)) || true; \
		rm -f $(PID_FILE); \
		echo "MCP server stopped."; \
	else \
		echo "MCP server is not running (PID file not found)."; \
	fi

# Check MCP Server Status
status:
	@if [ -f $(PID_FILE) ]; then \
		PID=$$(cat $(PID_FILE)); \
		if ps -p $$PID > /dev/null; then \
			echo "MCP server is running (PID: $$PID)"; \
		else \
			echo "MCP server is not running (PID file exists but process not found)"; \
			rm -f $(PID_FILE); \
		fi \
	else \
		echo "MCP server is not running"; \
	fi

# 3: Run Main Application
run:
	@echo "Starting Chat2K8s..."
	uv run main.py

# Clean build artifacts
clean:
	@if [ -f $(PID_FILE) ]; then \
		echo "Stopping MCP server before cleaning..."; \
		kill $$(cat $(PID_FILE)) || true; \
	fi
	rm -rf $(DIST_DIR)
