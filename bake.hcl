variable "PLATFORMS" {
  default = ["linux/amd64", "linux/arm64"]
}

variable "DOCKER_IMG" {
  default = "teslamate-speeding"
}

variable "DOCKER_IMG_TAG" {
  default = null
}

variable "DOCKER_IMG_REPO" {
  default = null
}

group "default" {
  targets = [
    "teslamate-speeding"
    ]
}

target "teslamate-speeding" {
  context = "./app"
  dockerfile = "Dockerfile"
  tags = ["${DOCKER_IMG_REPO}/${DOCKER_IMG}:latest", "${DOCKER_IMG_REPO}/${DOCKER_IMG}:${DOCKER_IMG_TAG}"]
  args = {
  }
  platforms = "${PLATFORMS}"
}
