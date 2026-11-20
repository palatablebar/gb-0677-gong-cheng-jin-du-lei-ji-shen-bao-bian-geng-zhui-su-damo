REASONS={"BAD_COLUMNS":"列数不匹配","EMPTY_VALUE":"必填值为空","INVALID_NUMBER":"数字格式错误","INVALID_TIME":"时间缺少偏移或格式错误","DUPLICATE_CONFLICT":"同键取值冲突","UNKNOWN_REFERENCE":"引用不存在","STALE_VERSION":"版本已更新","EMPTY_FILE":"文件为空","HEADER_ONLY":"仅有表头","BLANK_ONLY":"全部为空行",
"REVERSAL_REQUIRED":"负进度缺少已批冲回凭证","ORIGIN_NOT_FOUND":"冲回找不到原申报","EXCESS_REVERSAL":"冲回超过剩余可冲回量","DUPLICATE_VOUCHER":"同一冲回凭证在本批重复","REPLAY_VOUCHER":"冲回凭证已在已签批次使用"}
def reject(row, code, detail):
    return {**row,"reason":code,"detail":detail}
def sources(row):
    return row.get("_sources", [row.get("_line",0)])
