/**
 * axios 1.21 的返回类型是条件类型（`R extends AxiosResponseDefault ? AxiosResponse : R`），
 * 泛型调用点无法向 TS 证明返回值就是 T。运行时响应拦截器已经把信封剥成 data，
 * 所以这里只做一次显式的类型对齐，不隐藏任何转换。
 */
export function asPayload<T>(request: Promise<unknown>): Promise<T> {
  return request as Promise<T>
}
