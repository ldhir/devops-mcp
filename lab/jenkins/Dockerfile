# Jenkins with the Docker CLI, so the deploy job can build and restart the app.
FROM jenkins/jenkins:lts-jdk17
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*
RUN jenkins-plugin-cli --plugins configuration-as-code job-dsl workflow-aggregator git credentials-binding matrix-auth
ENV JAVA_OPTS="-Djenkins.install.runSetupWizard=false -Xmx512m"
ENV CASC_JENKINS_CONFIG=/var/jenkins_casc/jenkins.yaml
# Runs as root in the lab so it can use the Docker socket. Not how prod should run.
