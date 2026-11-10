/**
 * 上传文件过滤的纯函数。
 *
 * 放在 utils 而不是组件里：选文件夹时同一份判定要同时服务于「只收什么格式」与
 * 「隐藏文件不算用户主动选的语料」两类规则，写进模板就会变成测不到的字符串比较。
 */

/** 本步数据面接受的扩展名。与后端 `source_type_of` 的映射一一对应（html 不在选文件范围）。 */
const SUPPORTED_EXTENSIONS = new Set(['.md', '.markdown', '.txt', '.docx', '.pdf'])

/** 二进制解析路径（Worker 从落盘文件读字节）的格式。 */
const BINARY_EXTENSIONS = new Set(['.pdf', '.docx'])

/** 只取文件名本身的最后一段扩展名，`archive.tar.gz` 不算 gz 支持的文档。 */
function extensionOf(fileName: string): string {
  const basename = fileName.split(/[/\\]/).pop() ?? fileName
  const dot = basename.lastIndexOf('.')
  return dot <= 0 ? '' : basename.slice(dot).toLowerCase()
}

/**
 * 文件或路径里是否存在以点开头的段。
 *
 * 不能只看 `file.name`：`.git/config` 的 basename 是 `config`，而选文件夹时浏览器
 * 只把隐藏目录体现在 `webkitRelativePath` 里，不看它就等于把 `.git` 里的东西当语料。
 */
function hasHiddenSegment(path: string): boolean {
  return path
    .split(/[/\\]+/)
    .some((segment) => segment !== '' && segment !== '.' && segment !== '..' && segment.startsWith('.'))
}

export interface UploadFileCandidate {
  name: string
  /** 选文件夹时由浏览器填充；单文件选择为 undefined。 */
  webkitRelativePath?: string
}

/**
 * 选出可上传的候选：非隐藏 + 扩展名在准入列表。
 *
 * 判定只做「本地预筛」，真正的准入仍是后端按知识库 `allowed_formats` 决定
 * （比如 structure 库会拒绝 .txt）；这里提前过滤只是为了不让用户白等串行队列。
 */
export function isSupportedUploadFile(file: UploadFileCandidate): boolean {
  if (hasHiddenSegment(file.webkitRelativePath?.trim() || file.name)) {
    return false
  }
  return SUPPORTED_EXTENSIONS.has(extensionOf(file.name))
}

/** 该格式是否需要二进制解析（pdf/docx），供后续队列提示用。 */
export function isBinaryFile(fileName: string): boolean {
  return BINARY_EXTENSIONS.has(extensionOf(fileName))
}
