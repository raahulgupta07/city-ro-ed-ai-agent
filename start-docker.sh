#!/bin/bash

echo "======================================================"
echo "       RO-ED-Lang Docker Installation Script           "
echo "======================================================"
echo ""

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "ERROR: Docker is not running"
    echo "Please start Docker Desktop and try again"
    exit 1
fi

echo "Docker is running"
echo ""

# Check if .env file exists
if [ ! -f .env ]; then
    echo "Creating .env file..."
    cp .env.example .env
    echo ""
    echo "IMPORTANT: Edit .env and add your OPENROUTER_API_KEY"
    echo "   File location: .env"
    echo ""
    echo "Press Enter after you've added your API key..."
    read
fi

# Check if API key is set
if grep -q "sk-or-v1-your-openrouter-key-here" .env 2>/dev/null; then
    echo "ERROR: OpenRouter API key not set in .env"
    echo "Please edit .env and add your actual API key"
    exit 1
fi

# Check if JWT secret is set
if grep -q "CHANGE_ME_generate_with_openssl_rand_hex_32" .env 2>/dev/null; then
    echo "Generating JWT_SECRET_KEY..."
    JWT_KEY=$(openssl rand -hex 32)
    sed -i.bak "s/CHANGE_ME_generate_with_openssl_rand_hex_32/$JWT_KEY/" .env && rm -f .env.bak
    echo "JWT_SECRET_KEY generated and saved to .env"
fi

echo "Configuration verified"
echo ""

# Check if frontend is built
if [ ! -f frontend/build/index.html ] || [ ! -d frontend/build/_app ]; then
    echo "Frontend not built — building now..."
    if ! command -v node > /dev/null 2>&1; then
        echo "ERROR: Node.js is not installed"
        echo "Install Node.js (v18+) and try again"
        exit 1
    fi
    (cd frontend && npm install && npm run build)
    if [ $? -ne 0 ]; then
        echo "ERROR: Frontend build failed"
        exit 1
    fi
    echo "Frontend built successfully"
    echo ""
fi

# Build and start containers
echo "Building Docker containers..."
echo "This may take 3-5 minutes on first run..."
echo ""

docker-compose down 2>/dev/null
docker-compose up -d --build

if [ $? -eq 0 ]; then
    echo ""
    echo "======================================================"
    echo "            Installation Complete!                      "
    echo "======================================================"
    echo ""
    echo "Access the application:"
    echo "   App: http://localhost:9000"
    echo ""
    echo "Check status:"
    echo "   docker-compose ps"
    echo ""
    echo "View logs:"
    echo "   docker-compose logs -f"
    echo ""
    echo "Stop containers:"
    echo "   docker-compose down"
    echo ""
else
    echo ""
    echo "ERROR: Docker build failed"
    echo "Check the error messages above"
    echo "Try: docker-compose logs"
    exit 1
fi
