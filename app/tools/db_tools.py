"""
MySQL 数据库查询工具模块

封装数据库查询助手使用的三个 LangChain 工具：
list_sql_tables 用于发现真实表名，get_table_data 用于预览字段和样例数据，
execute_sql_query 用于在确认结构后执行自定义查询。

注意：计时/埋点/指标更新已由 observability_middleware 统一接管，
工具文件不再需要手工调用 time.perf_counter() 或 monitor.report_tool_end/failure()。
"""

import os

from dotenv import load_dotenv
from langchain_core.tools import tool
from mysql.connector import Error, connect

from app.api.monitor import monitor
from app.agent.recovery import resilient

load_dotenv()


# 集中读取数据库配置，后续三个工具都复用这份连接参数
def get_db_config():
    """
    从环境变量读取 MySQL 连接配置

    所有数据库工具都通过此函数拿到同一份连接参数，避免每个工具重复读取环境变量
    :return: mysql.connector.connect 可直接使用的连接参数
    """
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "charset": os.getenv("MYSQL_CHARSET", "utf8mb4"),
        "collation": os.getenv("MYSQL_COLLATION", "utf8mb4_unicode_ci"),
        "autocommit": True,
        "sql_mode": os.getenv("MYSQL_SQL_MODE", "TRADITIONAL"),
    }

    # 去掉未配置的可选项，避免把 None 传给 mysql.connector 造成连接参数异常
    config = {k: v for k, v in config.items() if v is not None}

    # user/password/database 是本教程工具能正常查询业务库的最小必要配置
    required_keys = ["user", "password", "database"]
    missing_keys = [k for k in required_keys if k not in config]
    if missing_keys:
        raise ValueError(f"缺失数据库核心配置：{', '.join(missing_keys)}")

    return config


@tool
@resilient(tool_name="list_sql_tables")
def list_sql_tables() -> str:
    """
    查询当前数据库中所有可用表

    作用：让模型先识别真实可用的表名，方便后续预览表结构和编写自定义 SQL。
    :return: 有表：可用的表有：表1,表2,表3...
             没有表：没有可用的表
             出现异常：查询出现异常：异常信息
    """

    # 埋点：工具一被调用，前端可以展示当前正在查询数据库表名
    monitor.report_tool(tool_name="数据库表名查询工具：list_sql_tables", args={})

    # 加载数据库连接信息
    config = get_db_config()

    # 异常(连接类瞬态 / SQL类可纠错 / 缺配置永久)由 @resilient 统一分类处理:
    # 瞬态退避重试,可纠错/永久回注 LLM 自纠错
    with connect(**config) as conn:
        with conn.cursor() as cursor:
            sql = "SHOW TABLES"
            cursor.execute(sql)

            # SHOW TABLES 返回形如：[("drugs",), ("inventory",), ("sales_records",)]
            tables = cursor.fetchall()
            if not tables:
                return "没有可用的表"

            # 取每个元组的第一个元素，拼成模型容易阅读的表名列表
            table_names = [table[0] for table in tables]
            return f"可用的表有：{', '.join(table_names)}"


@tool
@resilient(tool_name="get_table_data")
def get_table_data(table_name) -> str:
    """
    查询指定表的前 100 行数据

    当前工具调用之前，应先调用 list_sql_tables 完成表名校验。
    此工具的作用：
    1. 完成单表样例数据查询
    2. 为多表查询提供表结构信息和数据格式参考
    :param table_name: 表名
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             4. 至多查询 100 条表数据
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
                1,张三,18\n -> 至多查询 100 条
    """
    # 埋点：工具二被调用，前端可以展示当前正在预览哪张表
    monitor.report_tool(
        tool_name="数据库表数据查询工具：get_table_data",
        args={"table_name": table_name},
    )

    # 获取数据库参数
    config = get_db_config()

    # 只读 SELECT,瞬态连接错误可安全重试(@resilient 默认 max_retries=3)
    with connect(**config) as conn:
        with conn.cursor() as cursor:
            # 教程代码直接拼接表名，重点演示 Agent 查询链路；生产环境应改为白名单校验
            sql = f"SELECT * FROM {table_name} LIMIT 100"
            cursor.execute(sql)

            # cursor.description 保存查询结果的列元信息
            description = cursor.description
            if not description:
                return f"数据表 {table_name} 暂无数据。"

            # 只取每个列信息元组的第一个元素，也就是列名
            columns = [desc[0] for desc in description]

            # fetchall 返回表数据，形如：[(1, "张三", 18), (2, "李四", 20)]
            rows = cursor.fetchall()

            # 把每一行数据从元组转成 CSV 行文本
            results = [",".join(map(str, row)) for row in rows]

            # columns 组成 CSV 头部，rows 组成 CSV 数据体
            header_str = ",".join(columns)
            data_str = "\n".join(results)
            return f"{header_str}\n{data_str}"


@tool
@resilient(tool_name="execute_sql_query", max_retries=0)
def execute_sql_query(query) -> str:
    """
    执行自定义 SQL 查询

    切记：执行之前，需要通过 list_sql_tables 明确真实表名，
    再通过 get_table_data 明确表结构和数据格式。
    适合多表关联、筛选、聚合、排序等复杂查询。
    :param query: 要执行的自定义 SQL 语句
    :return: CSV 格式数据
             1. 第一行是列信息，列之间使用英文逗号分隔
             2. 第二行开始是表数据，值之间也使用英文逗号分隔
             3. 行和行之间使用 \n 分隔
             例如：
                id,name,age\n -> 列头
                1,张三,18\n
                1,张三,18\n
    """
    # 埋点：记录模型最终生成的 SQL，便于教学时观察是否真的落到了正确表字段上
    monitor.report_tool(
        tool_name="数据库表数据查询工具：execute_sql_query", args={"query": query}
    )

    # 获取数据库参数
    config = get_db_config()

    # max_retries=0:自定义 SQL 可能非幂等(UPDATE/DELETE),不重试,仅分类回注 LLM;
    # 熔断器仍会对瞬态连接失败计数,保护下游 MySQL
    with connect(**config) as conn:
        with conn.cursor() as cursor:
            # 当前章节依赖提示词约束模型生成只读查询；生产环境建议在工具层限制 SELECT/SHOW
            cursor.execute(query)

            # 非查询类 SQL 没有结果集描述，这里统一返回提示，避免工具调用直接抛错给模型
            description = cursor.description
            if not description:
                return f"执行自定义 SQL 语句没有查询结果，SQL 为：{query}"
            columns = [desc[0] for desc in description]

            rows = cursor.fetchall()
            results = [",".join(map(str, row)) for row in rows]

            header_str = ",".join(columns)
            data_str = "\n".join(results)
            return f"{header_str}\n{data_str}"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 .env 中的 MySQL 连接配置是否可用
    print(
        execute_sql_query.invoke(
            {
                "query": "SELECT * FROM `drugs` dgs join sales_records srd on dgs.drug_id = srd.drug_id and region = '华北区'"
            }
        )
    )
