import { useQuery } from '@tanstack/react-query'
import { Heart, ShoppingCart } from 'lucide-react'
import { useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import {
  DescriptionList,
  EmptyState,
  FieldError,
  PageHeader,
  StatusBadge,
} from '@/components/shared'
import { Button } from '@/components/ui/button'
import { Badge, Card, CardContent, Input, Label } from '@/components/ui/primitives'
import { useAuth } from '@/features/auth/auth-context'
import { useDebouncedValue } from '@/hooks/use-debounced-value'
import { useListQuery } from '@/hooks/use-list-query'
import { api } from '@/lib/api-client'
import { formatDate, formatMoney, formatQuantity, humanise } from '@/lib/format'
import { P } from '@/lib/permissions'
import { queryKeys, staleTimes } from '@/lib/query-client'
import type { SchemaOf } from '@/types/api'

import { dealerQuantity } from './dealer-payloads'
import { DEALER_API, dealerError, useDealerAction, useDealerRead } from './dealer-hooks'
import { DealerCredit, DealerImage, DealerPagination, DealerResult } from './dealer-shared'
import { DealerSpecificationFilters } from './dealer-specification-filters'

type CatalogItem = SchemaOf<'DealerCatalogItem'>

function AddToCart({
  itemId,
  variantId,
  disabled = false,
  initialQuantity = '1',
}: {
  itemId: string
  variantId?: string
  disabled?: boolean
  initialQuantity?: string
}) {
  const { can } = useAuth()
  const [quantity, setQuantity] = useState(initialQuantity)
  const [error, setError] = useState<string>()
  const action = useDealerAction(
    (value: string) =>
      api.post(`${DEALER_API}/cart/lines`, {
        item_id: itemId,
        item_variant_id: variantId || null,
        quantity: value,
      }),
    'Added to the cart',
  )
  if (!can(P.DEALER_PORTAL_ORDER)) return null
  return (
    <form
      className="mt-3 space-y-2"
      onSubmit={(event) => {
        event.preventDefault()
        try {
          setError(undefined)
          action.mutate(dealerQuantity(quantity))
        } catch (failure) {
          setError((failure as Error).message)
        }
      }}
    >
      <Label htmlFor={`quantity-${itemId}`}>Quantity</Label>
      <div className="flex flex-wrap gap-2">
        <Input
          id={`quantity-${itemId}`}
          inputMode="decimal"
          value={quantity}
          onChange={(event) => setQuantity(event.target.value)}
          className="min-h-11 w-24 min-w-0 grow"
          aria-invalid={Boolean(error)}
        />
        <Button type="submit" className="min-h-11 flex-1" disabled={disabled || action.isPending}>
          <ShoppingCart aria-hidden />
          Add to cart
        </Button>
      </div>
      <FieldError message={error || (action.error ? dealerError(action.error) : undefined)} />
    </form>
  )
}

function WishlistButton({ itemId, saved = false }: { itemId: string; saved?: boolean }) {
  const action = useDealerAction(
    () => api.post(`${DEALER_API}/wishlist`, { item_id: itemId }),
    'Saved to the wishlist',
  )
  return (
    <div>
      <Button
        type="button"
        variant="outline"
        className="min-h-11"
        disabled={saved || action.isPending}
        onClick={() => action.mutate()}
        aria-label={saved ? 'Saved to wishlist' : 'Save to wishlist'}
      >
        <Heart aria-hidden className={saved ? 'fill-current' : ''} />
        {saved ? 'Saved' : 'Save'}
      </Button>
      <FieldError message={action.error ? dealerError(action.error) : undefined} />
    </div>
  )
}

export function DealerItemCard({ item }: { item: CatalogItem }) {
  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <Link to={`/dealer/catalog/${item.id}`} className="block space-y-3">
          <DealerImage
            path={item.primary_image_id ? `${DEALER_API}/images/${item.primary_image_id}` : null}
            label={item.name}
          />
          <div>
            <p className="text-xs text-muted-foreground">{item.code}</p>
            <h2 className="font-semibold">{item.name}</h2>
          </div>
        </Link>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="font-semibold">
            {item.price_on_request ? 'Price on request' : formatMoney(item.price)}
          </p>
          <StatusBadge status={item.availability_band} />
        </div>
        {item.best_offer && (
          <Badge variant="success" className="max-w-full whitespace-normal break-words text-left">
            {formatQuantity(item.best_offer.discount_percentage)}% off · {item.best_offer.title}
          </Badge>
        )}
        <WishlistButton itemId={item.id} saved={item.in_wishlist} />
        <AddToCart itemId={item.id} disabled={!item.orderable} />
      </CardContent>
    </Card>
  )
}

export function DealerCatalogPage() {
  const [searchParams] = useSearchParams()
  const categories = useDealerRead<SchemaOf<'DealerCategoryNode'>[]>('catalog/categories')
  const brands = useDealerRead<SchemaOf<'DealerBrandRead'>[]>('catalog/brands')
  const list = useListQuery<CatalogItem>({
    path: `${DEALER_API}/catalog/items`,
    queryKey: (params) => queryKeys.dealerPortal.list('catalog', params),
    defaultLimit: 12,
    defaultSortBy: 'code',
    staleTime: staleTimes.live,
    filters: {
      category_id: searchParams.get('category_id') || undefined,
      brand_id: searchParams.get('brand_id') || undefined,
      in_stock_only: searchParams.get('in_stock_only') === 'true',
      on_offer_only: searchParams.get('on_offer_only') === 'true',
      spec: searchParams.getAll('spec'),
      ...(searchParams.get('has_accessories') === 'true' ||
      searchParams.get('has_accessories') === 'false'
        ? { has_accessories: searchParams.get('has_accessories') === 'true' }
        : {}),
    },
  })
  return (
    <>
      <PageHeader
        title="Catalog"
        description="Your channel prices, current offers and product availability."
        actions={
          <Button variant="outline" asChild>
            <Link to="/dealer/wishlist">Wishlist</Link>
          </Button>
        }
      />
      <div className="mb-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <Label htmlFor="catalog-search">Search catalog</Label>
          <Input
            id="catalog-search"
            value={list.search}
            onChange={(event) => list.setSearch(event.target.value)}
            maxLength={120}
            className="mt-1 min-h-11"
          />
        </div>
        <div>
          <Label htmlFor="catalog-category">Category</Label>
          <select
            id="catalog-category"
            value={searchParams.get('category_id') || ''}
            onChange={(event) => list.setParam('category_id', event.target.value || undefined)}
            className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
          >
            <option value="">All categories</option>
            {categories.data?.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <Label htmlFor="catalog-brand">Brand</Label>
          <select
            id="catalog-brand"
            value={searchParams.get('brand_id') || ''}
            onChange={(event) => list.setParam('brand_id', event.target.value || undefined)}
            className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
          >
            <option value="">All brands</option>
            {brands.data?.map((brand) => (
              <option key={brand.id} value={brand.id}>
                {brand.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <Label htmlFor="catalog-sort">Sort by</Label>
          <select
            id="catalog-sort"
            value={list.sortBy}
            onChange={(event) => list.setSort(event.target.value, 'asc')}
            className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
          >
            <option value="code">Code</option>
            <option value="name">Name</option>
          </select>
        </div>
        <label className="flex min-h-11 items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={searchParams.get('in_stock_only') === 'true'}
            onChange={(event) =>
              list.setParam('in_stock_only', event.target.checked ? 'true' : undefined)
            }
          />
          Available items only
        </label>
        <label className="flex min-h-11 items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={searchParams.get('on_offer_only') === 'true'}
            onChange={(event) =>
              list.setParam('on_offer_only', event.target.checked ? 'true' : undefined)
            }
          />
          On offer
        </label>
        <div>
          <Label htmlFor="catalog-accessories">Accessories</Label>
          <select
            id="catalog-accessories"
            className="mt-1 min-h-11 w-full rounded-md border bg-background px-3 text-sm"
            value={searchParams.get('has_accessories') || ''}
            onChange={(event) => list.setParam('has_accessories', event.target.value || undefined)}
          >
            <option value="">Any accessories</option>
            <option value="true">Has accessories</option>
            <option value="false">No accessories</option>
          </select>
        </div>
      </div>
      <DealerSpecificationFilters />
      <DealerResult loading={list.isLoading} error={list.error}>
        {list.data.length ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {list.data.map((item) => (
              <DealerItemCard key={item.id} item={item} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="No matching products"
            description="Try another search or clear the filters."
          />
        )}
        <DealerPagination pagination={list.pagination} page={list.page} onPage={list.setPage} />
      </DealerResult>
    </>
  )
}

export function DealerItemPage() {
  const { itemId = '' } = useParams()
  const detail = useDealerRead<SchemaOf<'DealerCatalogItemDetail'>>(
    `catalog/items/${itemId}`,
    Boolean(itemId),
  )
  const [quantity, setQuantity] = useState('1')
  const [variant, setVariant] = useState('')
  const debouncedQuantity = useDebouncedValue(quantity)
  let validQuantity = false
  try {
    dealerQuantity(debouncedQuantity)
    validQuantity = true
  } catch {
    /* Keep an incomplete quantity local until the user finishes typing. */
  }
  const quote = useQuery({
    queryKey: queryKeys.dealerPortal.price(itemId, debouncedQuantity, variant),
    queryFn: ({ signal }) =>
      api.get<SchemaOf<'DealerPriceQuote'>>(`${DEALER_API}/catalog/items/${itemId}/price`, {
        params: { quantity: debouncedQuantity, variant_id: variant || undefined },
        signal,
      }),
    enabled: Boolean(itemId) && validQuantity,
    staleTime: staleTimes.live,
  })
  const item = detail.data
  return (
    <>
      <PageHeader
        title={item?.name || 'Product'}
        breadcrumbs={[
          { label: 'Catalog', to: '/dealer/catalog' },
          { label: item?.code || 'Product' },
        ]}
      />
      <DealerResult loading={detail.isLoading} error={detail.error}>
        {item && (
          <div className="space-y-6">
            <div className="grid gap-5 md:grid-cols-2">
              <div className="space-y-3">
                <DealerImage
                  path={
                    item.primary_image_id ? `${DEALER_API}/images/${item.primary_image_id}` : null
                  }
                  label={item.name}
                />
                <div className="grid grid-cols-3 gap-2">
                  {item.image_ids
                    .filter((id) => id !== item.primary_image_id)
                    .map((id) => (
                      <DealerImage key={id} path={`${DEALER_API}/images/${id}`} label={item.name} />
                    ))}
                </div>
              </div>
              <div className="space-y-4">
                <StatusBadge status={item.availability_band} />
                <p className="text-sm text-muted-foreground">{item.description}</p>
                <DescriptionList
                  items={[
                    { label: 'Code', value: item.code },
                    { label: 'HSN', value: item.hsn_code || '—' },
                    { label: 'GST', value: `${formatQuantity(item.gst_rate)}%` },
                    {
                      label: 'Returns',
                      value: item.can_return ? 'Eligible subject to approval' : 'Not returnable',
                    },
                  ]}
                />
                {item.variants.length > 0 && (
                  <div>
                    <Label htmlFor="dealer-variant">Variant</Label>
                    <select
                      id="dealer-variant"
                      value={variant}
                      onChange={(event) => setVariant(event.target.value)}
                      className="mt-1 min-h-11 w-full rounded-md border bg-background px-3"
                    >
                      <option value="">Standard item</option>
                      {item.variants
                        .filter((entry) => entry.is_active)
                        .map((entry) => (
                          <option key={entry.id} value={entry.id}>
                            {entry.code}
                          </option>
                        ))}
                    </select>
                  </div>
                )}
                <div>
                  <Label htmlFor="dealer-price-quantity">Quantity for price quote</Label>
                  <Input
                    id="dealer-price-quantity"
                    inputMode="decimal"
                    value={quantity}
                    onChange={(event) => setQuantity(event.target.value)}
                    className="mt-1 min-h-11"
                  />
                  <p className="mt-2 text-xl font-semibold">
                    {quote.data && !quote.data.price_on_request
                      ? formatMoney(quote.data.net_unit)
                      : 'Price on request'}
                  </p>
                  {quote.data?.offer && (
                    <p className="text-sm text-success">
                      {quote.data.offer.title} · {formatQuantity(quote.data.discount_percentage)}%
                      off
                    </p>
                  )}
                </div>
                <WishlistButton itemId={item.id} saved={item.in_wishlist} />
                <AddToCart
                  key={`${item.id}-${variant}-${debouncedQuantity}`}
                  itemId={item.id}
                  variantId={variant}
                  disabled={!item.orderable}
                  initialQuantity={validQuantity ? debouncedQuantity : '1'}
                />
              </div>
            </div>
            {item.slabs.length > 0 && (
              <section>
                <h2 className="mb-3 font-semibold">Quantity pricing</h2>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <caption className="sr-only">Channel price slabs</caption>
                    <thead>
                      <tr>
                        <th className="p-2 text-left">Minimum quantity</th>
                        <th className="p-2 text-right">Unit price</th>
                      </tr>
                    </thead>
                    <tbody>
                      {item.slabs.map((slab, index) => (
                        <tr key={index} className="border-t">
                          <td className="p-2">{formatQuantity(slab.min_quantity)}</td>
                          <td className="p-2 text-right">{formatMoney(slab.price)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
            {item.specifications.length > 0 && (
              <section>
                <h2 className="mb-3 font-semibold">Specifications</h2>
                <DescriptionList
                  items={item.specifications.map((specification) => ({
                    label: specification.name,
                    value: `${specification.value}${specification.unit ? ` ${specification.unit}` : ''}`,
                  }))}
                />
              </section>
            )}
            {item.accessories.length > 0 && (
              <section>
                <h2 className="mb-3 font-semibold">Accessories and in the box</h2>
                <div className="grid gap-3 sm:grid-cols-2">
                  {item.accessories.map((accessory) => (
                    <Card
                      key={accessory.accessory_item_id}
                      role="group"
                      aria-label={accessory.name}
                    >
                      <CardContent className="p-4">
                        {accessory.in_the_box ? (
                          <p className="font-medium">{accessory.name}</p>
                        ) : (
                          <Link
                            to={`/dealer/catalog/${accessory.accessory_item_id}`}
                            className="font-medium underline"
                          >
                            {accessory.name}
                          </Link>
                        )}
                        <p className="mt-1 text-sm text-muted-foreground">
                          {accessory.in_the_box ? 'In the box' : humanise(accessory.relation)} ·{' '}
                          {formatQuantity(accessory.quantity)}
                        </p>
                        {!accessory.in_the_box && (
                          <>
                            <p className="mt-2">
                              {accessory.price_on_request
                                ? 'Price on request'
                                : formatMoney(accessory.price)}
                            </p>
                            <AddToCart
                              itemId={accessory.accessory_item_id}
                              initialQuantity={accessory.quantity}
                              disabled={accessory.price_on_request}
                            />
                          </>
                        )}
                      </CardContent>
                    </Card>
                  ))}
                </div>
              </section>
            )}
          </div>
        )}
      </DealerResult>
    </>
  )
}

export function DealerHomePage() {
  const { can } = useAuth()
  const me = useDealerRead<SchemaOf<'DealerPortalMe'>>('me')
  const categories = useDealerRead<SchemaOf<'DealerCategoryNode'>[]>('catalog/categories')
  const brands = useDealerRead<SchemaOf<'DealerBrandRead'>[]>('catalog/brands')
  return (
    <>
      <PageHeader
        title={me.data ? `Welcome, ${me.data.customer_name}` : 'Dealer home'}
        description="Browse your catalog and manage orders and account activity."
      />
      <DealerResult loading={me.isLoading} error={me.error}>
        {me.data && <DealerCredit credit={me.data.credit} />}
      </DealerResult>
      <DealerHomePromotions />
      <section className="mt-6">
        <h2 className="mb-3 font-semibold">Shop by category</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {categories.data?.map((category) => (
            <Button
              key={category.id}
              asChild
              variant="outline"
              className="min-h-14 whitespace-normal"
            >
              <Link to={`/dealer/catalog?category_id=${category.id}`}>{category.name}</Link>
            </Button>
          ))}
        </div>
      </section>
      <section className="mt-6">
        <h2 className="mb-3 font-semibold">Brands</h2>
        <div className="flex flex-wrap gap-3">
          {brands.data?.map((brand) => (
            <Button key={brand.id} asChild variant="outline" className="min-h-11">
              <Link to={`/dealer/catalog?brand_id=${brand.id}`}>{brand.name}</Link>
            </Button>
          ))}
        </div>
      </section>
      <div className="mt-6 flex flex-wrap gap-3">
        <Button asChild className="min-h-11">
          <Link to="/dealer/catalog">Browse catalog</Link>
        </Button>
        {can(P.DEALER_PORTAL_ORDER) && (
          <Button asChild variant="outline" className="min-h-11">
            <Link to="/dealer/orders">Your orders</Link>
          </Button>
        )}
        {can(P.DEALER_PORTAL_ACCOUNT) && (
          <Button asChild variant="outline" className="min-h-11">
            <Link to="/dealer/account">Your account</Link>
          </Button>
        )}
      </div>
      {can(P.DEALER_PORTAL_ORDER) && <DealerRecentOrders />}
    </>
  )
}

function DealerHomePromotions() {
  const banners = useListQuery<SchemaOf<'DealerBannerRead'>>({
    path: `${DEALER_API}/banners`,
    queryKey: (params) => queryKeys.dealerPortal.list('banners', params),
    namespace: 'home_banners',
    defaultLimit: 5,
  })
  const offers = useListQuery<SchemaOf<'DealerOfferRead'>>({
    path: `${DEALER_API}/offers`,
    queryKey: (params) => queryKeys.dealerPortal.list('offers', params),
    namespace: 'home_offers',
    defaultLimit: 6,
  })
  const [index, setIndex] = useState(0)
  const slide = banners.data[Math.min(index, banners.data.length - 1)]
  const destination =
    slide?.link_kind === 'ITEM'
      ? `/dealer/catalog/${slide.link_id}`
      : slide?.link_kind === 'CATEGORY'
        ? `/dealer/catalog?category_id=${slide.link_id}`
        : slide?.link_kind === 'BRAND'
          ? `/dealer/catalog?brand_id=${slide.link_id}`
          : slide?.link_kind === 'OFFER'
            ? '/dealer/catalog?on_offer_only=true'
            : null
  return (
    <div className="mt-6 space-y-5">
      <DealerResult loading={banners.isLoading} error={banners.error}>
        {slide && (
          <section
            role="region"
            aria-roledescription="carousel"
            aria-label="Promotions"
            className="rounded-lg border p-4"
          >
            <div
              aria-roledescription="slide"
              aria-label={`Banner ${index + 1}`}
              className="grid items-center gap-4 md:grid-cols-2"
            >
              <DealerImage
                path={`${DEALER_API}/images/${slide.image_document_id}`}
                label={slide.title}
              />
              <div>
                <h2 className="text-lg font-semibold">{slide.title}</h2>
                {slide.subtitle && (
                  <p className="mt-2 text-sm text-muted-foreground">{slide.subtitle}</p>
                )}
                {destination && (
                  <Button asChild className="mt-3 min-h-11">
                    <Link to={destination}>View products</Link>
                  </Button>
                )}
              </div>
            </div>
            <div className="mt-3 flex justify-between gap-3">
              <Button
                variant="outline"
                aria-label="Previous banner"
                disabled={index === 0 && banners.page === 1}
                onClick={() => {
                  if (index > 0) setIndex(index - 1)
                  else {
                    banners.setPage(banners.page - 1)
                    setIndex(0)
                  }
                }}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                aria-label="Next banner"
                disabled={index >= banners.data.length - 1 && !banners.pagination?.has_next}
                onClick={() => {
                  if (index < banners.data.length - 1) setIndex(index + 1)
                  else {
                    banners.setPage(banners.page + 1)
                    setIndex(0)
                  }
                }}
              >
                Next
              </Button>
            </div>
          </section>
        )}
      </DealerResult>
      <DealerResult loading={offers.isLoading} error={offers.error}>
        {offers.data.length > 0 && (
          <section>
            <h2 className="mb-3 font-semibold">Current offers</h2>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {offers.data.map((offer) => (
                <Card key={offer.id}>
                  <CardContent className="space-y-2 p-4">
                    <h3 className="font-medium">{offer.title}</h3>
                    <Badge variant="success">
                      {formatQuantity(offer.discount_percentage)}% off
                    </Badge>
                    <p className="text-sm text-muted-foreground">
                      From {formatQuantity(offer.min_quantity)} units
                      {offer.valid_to ? ` · Ends ${formatDate(offer.valid_to)}` : ''}
                    </p>
                    <Link
                      className="inline-flex min-h-11 items-center text-sm underline"
                      to={
                        offer.item_id
                          ? `/dealer/catalog/${offer.item_id}`
                          : offer.category_id
                            ? `/dealer/catalog?category_id=${offer.category_id}&on_offer_only=true`
                            : offer.brand_id
                              ? `/dealer/catalog?brand_id=${offer.brand_id}&on_offer_only=true`
                              : '/dealer/catalog?on_offer_only=true'
                      }
                    >
                      Browse offer products
                    </Link>
                  </CardContent>
                </Card>
              ))}
            </div>
            <DealerPagination
              pagination={offers.pagination}
              page={offers.page}
              onPage={offers.setPage}
            />
          </section>
        )}
      </DealerResult>
    </div>
  )
}

function DealerRecentOrders() {
  const orders = useListQuery<SchemaOf<'DealerOrderSummary'>>({
    path: `${DEALER_API}/orders`,
    queryKey: (params) => queryKeys.dealerPortal.list('recent-orders', params),
    namespace: 'home_orders',
    defaultLimit: 5,
    defaultSortBy: 'created_at',
    defaultSortDir: 'desc',
    staleTime: staleTimes.transactional,
  })
  return (
    <section className="mt-6">
      <h2 className="mb-3 font-semibold">Recent orders</h2>
      <DealerResult loading={orders.isLoading} error={orders.error}>
        <div className="space-y-2">
          {orders.data.map((order) => (
            <Link
              key={order.id}
              to={`/dealer/orders/${order.id}`}
              className="flex min-h-14 flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
            >
              <span className="font-medium">{order.code}</span>
              <StatusBadge status={order.status} />
              <span className="text-sm">
                {formatMoney(order.total_amount, { currency: order.currency })}
              </span>
            </Link>
          ))}
          {!orders.data.length && (
            <p className="text-sm text-muted-foreground">
              Your orders will appear here after checkout.
            </p>
          )}
        </div>
      </DealerResult>
    </section>
  )
}
