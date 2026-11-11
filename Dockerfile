# Same OS as the lab servers (Ubuntu 22.04). Build:   docker build -t tpch .
# Data lives in a volume; generate/load at run time (needs the server running):
#   docker run -it --privileged -v tpch-data:/srv/tpch tpch src/setup/setup.sh cluster data
# --privileged is only needed for measurements (drop_caches), not for setup.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive TZ=Asia/Tokyo LANG=C.UTF-8
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends sudo ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/tpch
COPY . .
RUN src/setup/setup.sh sys pg py dbgen && rm -rf /var/lib/apt/lists/*
ENV PATH=/opt/tpch/.venv/bin:$PATH
CMD ["bash"]
