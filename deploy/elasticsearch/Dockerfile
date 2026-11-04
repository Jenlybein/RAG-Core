# 官方 ES 8.17.10 + IK 中文分词器（8.17.10，必须同版）
#
# 为什么不用现成镜像：教程原先指向的第三方预装 IK 镜像
# registry.cn-hangzhou.aliyuncs.com/tulingfox/elasticsearch:8.17.10
# 已无法匿名拉取（ACR 走完匿名 token 后仍返回 not found），
# 因此改为官方镜像自建一层，把「装插件」从一次性操作变成可复现的构建步骤。
FROM docker.elastic.co/elasticsearch/elasticsearch:8.17.10

RUN bin/elasticsearch-plugin install --batch \
    https://release.infinilabs.com/analysis-ik/stable/elasticsearch-analysis-ik-8.17.10.zip
