# Delta Dental Infra — Task Management API Specification

## Overview

A FastAPI-based task management REST API backed by Microsoft SQL Server (via PyODBC).
The app is containerized with Docker and deployed to Azure AKS via ArgoCD.

---

## API Endpoints

### 1. `GET /`
- **Purpose**: Initialize the database — create the `Tasks` table if it doesn't exist.
- **Response**: `"Add-Tasks API Ready."` on success (including if table already exists).
- **Error handling**: If any error other than "table already exists" occurs, return `"Error. Please check Logs."` and print the exception.

### 2. `GET /tasks`
- **Purpose**: List all tasks from the database.
- **Response**: JSON array of task objects `[{id, title, description}, ...]`.
- **Must** query `SELECT * FROM Tasks` and return all rows.

### 3. `GET /tasks/{task_id}`
- **Purpose**: Retrieve a single task by its ID.
- **Path parameter**: `task_id` (integer)
- **Response**: JSON object `{id, title, description}`.
- **Error**: Return HTTP 404 if the task is not found.

### 4. `POST /tasks`
- **Purpose**: Create a new task.
- **Request body**: `{"title": "string", "description": "string"}`
- **Validation**: Both `title` and `description` are required; `title` must be non-empty and <= 200 characters.
- **Response**: The created task object (should include the generated `id`).
- **Must** use parameterized queries (no SQL injection).

### 5. `PUT /tasks/{task_id}`
- **Purpose**: Update an existing task.
- **Path parameter**: `task_id` (integer)
- **Request body**: `{"title": "string", "description": "string"}`
- **Error**: Return HTTP 404 if the task doesn't exist.
- **Response**: The updated task object.

### 6. `DELETE /tasks/{task_id}`
- **Purpose**: Delete a task by ID.
- **Path parameter**: `task_id` (integer)
- **Error**: Return HTTP 404 if the task doesn't exist.
- **Response**: Confirmation message.

---

## Data Model

### Tasks Table
| Column      | Type          | Constraints                 |
|-------------|---------------|-----------------------------|
| ID          | int           | PRIMARY KEY, IDENTITY       |
| Title       | varchar(255)  | NOT NULL                    |
| Description | text          |                             |

---

## Configuration

- **Connection string**: Read from `CONNECTION_STRING` environment variable.
- The app **must** fail gracefully if `CONNECTION_STRING` is not set (log a warning, don't crash).
- CORS must be configured (currently allows all origins for dev).

---

## Infrastructure (Terraform)

### Modules
- `azurerm_resource_group` — Azure Resource Group
- `azurerm_acr` — Azure Container Registry
- `azurerm_aks` — Azure Kubernetes Service cluster
- `azurerm_Database` — Azure SQL Database

### Environment
- `environments/dev/` — Dev environment using the above modules.

---

## Deployment Pipeline

### CI/CD (`main.yaml`)
1. Checkout code
2. Azure Login (OIDC)
3. Login to ACR
4. Build & push `add-task` Docker image
5. Build & push `micro-ui` Docker image
6. Update Kubernetes deployment manifest with new image tag
7. Commit and push updated manifest (triggers ArgoCD sync)

### ArgoCD
- `argocd/application.yaml` — Watches the repo for manifest changes and syncs to AKS.

---

## Testing Requirements

- All API endpoints must have basic integration or unit tests.
- The app should handle database connection errors gracefully.
- Dockerfile must build without errors.
