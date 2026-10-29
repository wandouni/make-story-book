import { api } from '@/api/client'
import type {
  Book,
  BookSummary,
  CreateBookRequest,
  UpdateBookRequest,
} from '@/types/book'

export const listBooks = () => api.get<BookSummary[]>('/books').then((r) => r.data)

export const createBook = (payload: CreateBookRequest) =>
  api.post<Book>('/books', payload).then((r) => r.data)

export const getBook = (id: string) => api.get<Book>(`/books/${id}`).then((r) => r.data)

export const updateBook = (id: string, payload: UpdateBookRequest) =>
  api.patch<Book>(`/books/${id}`, payload).then((r) => r.data)

export const deleteBook = (id: string) =>
  api.delete<{ ok: boolean }>(`/books/${id}`).then((r) => r.data)
