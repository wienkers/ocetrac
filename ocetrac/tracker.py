import xarray as xr
import numpy as np
import scipy.ndimage
from skimage.measure import regionprops_table 
from skimage.measure import label as label_np
import dask.array as dsa

def _apply_mask(binary_images, mask):
    binary_images_with_mask = binary_images.where(mask==1, drop=False, other=0)
    return binary_images_with_mask

class Tracker:
        
    def __init__(self, da, mask, radius, min_size_quartile, timedim, xdim, ydim, positive=True):
        
        

        self.da = da
        self.mask = mask
        self.radius = radius
        self.min_size_quartile = min_size_quartile
        self.timedim = timedim
        self.xdim = xdim
        self.ydim = ydim   
        self.positive = positive
        
        if ((timedim, ydim, xdim) != da.dims):
            try:
                da = da.transpose(timedim, ydim, xdim) 
            except:
                raise ValueError(f'Ocetrac currently only supports 3D DataArrays. The dimensions should only contain ({timedim}, {xdim}, and {ydim}). Found {list(da.dims)}')

            
    def track(self):
        '''
        Label and track image features.
        
        Parameters
        ----------
        da : xarray.DataArray
            The data to label.

        mask : xarray.DataArray
            The mask of ponts to ignore. Must be binary where 1 = true point and 0 = background to be ignored. 

        radius : int
            The size of the structuring element used in morphological opening and closing. Radius specified by the number of grid units.

        min_size_quartile : float
            The quantile used to define the threshold of the smallest area object retained in tracking. Value should be between 0 and 1.

        timedim : str
            The name of the time dimension
        
        xdim : str
            The name of the x dimension

        ydim : str
            The namne of the y dimension
            
        positive : bool
            True if da values are expected to be positive, false if they are negative. Default argument is True

        Returns
        -------
        labels : xarray.DataArray
            Integer labels of the connected regions.
        '''

        if (self.mask == 0).all():
            raise ValueError('Found only zeros in `mask` input. The mask should indicate valid regions with values of 1')

        # Convert data to binary, define structuring element, and perform morphological closing then opening
        binary_images = self._morphological_operations()

        # Apply mask
        binary_images_with_mask  = _apply_mask(binary_images,self.mask) # perhaps change to method? JB

        # Filter area
        area, min_area, binary_labels, N_initial = self._filter_area(binary_images_with_mask)

        # Label objects
        labels = self._label_either(binary_labels, connectivity=3)
        labels = xr.DataArray(labels, dims=binary_labels.dims, coords=binary_labels.coords).chunk(binary_images_with_mask.chunks)
        
        # Wrap labels
        grid_res = abs(self.da[self.xdim][1]-self.da[self.xdim][0])
        if self.da[self.xdim][-1]-self.da[self.xdim][0] >= 360-grid_res:
            labels_wrapped, N_final = self._wrap(labels)
        else:
            labels_wrapped = labels
            N_final = np.max(labels)
                

        # Final labels to DataArray
        new_labels = xr.DataArray(labels_wrapped, dims=self.da.dims, coords=self.da.coords)   
        new_labels = new_labels.where(new_labels!=0, drop=False, other=np.nan)


        ## Metadata

        # Calculate Percent of total object area retained after size filtering
        sum_tot_area = int(np.sum(area.values))

        reject_area = area.where(area<=min_area, drop=True)
        sum_reject_area = int(np.sum(reject_area.values))
        percent_area_reject = (sum_reject_area/sum_tot_area)

        accept_area = area.where(area>min_area, drop=True)
        sum_accept_area = int(np.sum(accept_area.values))
        percent_area_accept = (sum_accept_area/sum_tot_area)

        new_labels = new_labels.rename('labels')
        new_labels.attrs['inital objects identified'] = int(N_initial)
        new_labels.attrs['final objects tracked'] = int(N_final)
        new_labels.attrs['radius'] = self.radius
        new_labels.attrs['size quantile threshold'] = self.min_size_quartile
        new_labels.attrs['min area'] = min_area
        new_labels.attrs['percent area reject'] = percent_area_reject
        new_labels.attrs['percent area accept'] = percent_area_accept

        print('inital objects identified \t', int(N_initial))
        print('final objects tracked \t', int(N_final))

        return new_labels


    ### PRIVATE METHODS - not meant to be called by user ###
    

    def _morphological_operations(self): 
        '''Converts xarray.DataArray to binary, defines structuring element, and performs morphological closing then opening.
        Parameters
        ----------
        da     : xarray.DataArray
                The data to label
        radius : int
                Length of grid spacing to define the radius of the structing element used in morphological closing and opening.

        '''

        # Convert images to binary. All positive values == 1, otherwise == 0
        if self.positive == True:
            bitmap_binary = self.da.where(self.da>0, drop=False, other=0)
        
        elif self.positive == False:
            bitmap_binary = self.da.where(self.da<0, drop=False, other=0)
    
        bitmap_binary = bitmap_binary.where(bitmap_binary==0, drop=False, other=1)

        # Define structuring element
        diameter = self.radius*2
        x = np.arange(-self.radius, self.radius+1)
        x, y = np.meshgrid(x, x)
        r = x**2+y**2 
        se = r<self.radius**2

        def binary_open_close(bitmap_binary):
            bitmap_binary_padded = np.pad(bitmap_binary,
                                          ((diameter, diameter), (diameter, diameter)),
                                          mode='wrap')
            s1 = scipy.ndimage.binary_closing(bitmap_binary_padded, se, iterations=1)
            s2 = scipy.ndimage.binary_opening(s1, se, iterations=1)
            unpadded= s2[diameter:-diameter, diameter:-diameter]
            return unpadded

        mo_binary = xr.apply_ufunc(binary_open_close, bitmap_binary,
                                   input_core_dims=[[self.ydim, self.xdim]],
                                   output_core_dims=[[self.ydim, self.xdim]],
                                   output_dtypes=[bitmap_binary.dtype],
                                   vectorize=True,
                                   dask='parallelized')
        return mo_binary


    def _filter_area(self, binary_images):
        '''calculatre area with regionprops'''

        def get_labels(binary_images):
            blobs_labels = self._label_either(binary_images, background=0)
            return blobs_labels

        labels = xr.apply_ufunc(get_labels, binary_images,
                                input_core_dims=[[self.ydim, self.xdim]],
                                output_core_dims=[[self.ydim, self.xdim]],
                                output_dtypes=[binary_images.dtype],
                                vectorize=True,
                                dask='parallelized')


        labels = xr.DataArray(labels, dims=binary_images.dims, coords=binary_images.coords)
        labels = labels.where(labels>0, drop=False, other=np.nan)  

        # The labels are repeated each time step, therefore we relabel them to be consecutive
        # This is embarrasingly parallel in time, so this removes the serialising time dependence...
        cumulative_max = labels.max(dim={self.xdim,self.ydim}).cumsum(dim=self.timedim)
        labels = labels + cumulative_max.shift({self.timedim: 1}, fill_value=0)

        labels = labels.where(labels>0, drop=False, other=0)  
        labels_wrapped, N_initial = self._wrap(labels)

        # Calculate Area of each object and keep objects larger than threshold
        props = regionprops_table(labels_wrapped.values.astype('int'), properties=['label', 'area'])
        
        labelprops = props['label']
        labelprops = xr.DataArray(labelprops, dims=['label'], coords={'label': labelprops}) 
        
        area = props['area']
        area = xr.DataArray(area, dims=['label'], coords={'label': labelprops}) 

        if area.size == 0:
            raise ValueError(f'No objects were detected. Try changing radius or min_size_quartile parameters.')
        
        min_area = np.percentile(area, self.min_size_quartile*100)
        print(f'minimum area: {min_area}') 
        
        keep_labels = labelprops.where(area>=min_area, drop=True)
        keep_where = np.isin(labels_wrapped, keep_labels)
        out_labels = xr.DataArray(np.where(keep_where==False, 0, labels_wrapped), dims=binary_images.dims, coords=binary_images.coords) #.chunk(binary_images.chunks)

        # Convert images to binary. All positive values == 1, otherwise == 0
        binary_labels = out_labels.where(out_labels==0, drop=False, other=1)

        return area, min_area, binary_labels, N_initial


    def _label_either(self, data, **kwargs):
        if isinstance(data, dsa.Array):
            try:
                from dask_image.ndmeasure import label as label_dask
                def label_func(a, **kwargs):
                    ids, num = label_dask(a, **kwargs)
                    return ids
            except ImportError:
                raise ImportError(
                    "Dask_image is required to use this function on Dask arrays. "
                    "Either install dask_image or else call .load() on your data."
                )
        else:
            label_func = label_np
        return label_func(data, **kwargs)


    def _wrap(self, labels):
        ''' Impose periodic boundary and wrap labels... daskified'''
        
        ## This is embarrasingly parallel in time, so we can use dask to avoid the task dependencies...
                
        # Wrap the wrap wrapper 
        def wrap_x(labels):  # Should be vectorised -- i.e. working on numpy arrays
            labels_wrapped = labels.copy() # input is immutable from dask ufunc...
            first_column = labels[:, 0]  # all of y, first x
            last_column = labels[:, -1]
            unique_first = np.unique(first_column[first_column>0])
            
            for value in unique_first:
                first = np.where(first_column == value)[0]  # all are 1d now... select just the first index of the tuple
                last = last_column[first]
                bad_labels = np.unique(last[last>0])
                replace = np.isin(labels, bad_labels)
                if replace.size == 0:
                    continue
                
                labels_wrapped[replace] = value
            
            return labels_wrapped
        
        
        labels_wrapped = xr.apply_ufunc(wrap_x, labels,
                                input_core_dims=[[self.ydim, self.xdim]],
                                output_core_dims=[[self.ydim, self.xdim]],
                                output_dtypes=[labels.dtype],
                                vectorize=True,
                                dask='parallelized')

        def inverse_unique(labels):
    
            labels_dask = labels.data

            unique_values = dsa.unique(labels_dask)   # .data makes this a dask array (not xarray!)
            
            # Map blocks to indices
            def map_to_indices(block, unique_values):
                return np.searchsorted(unique_values, block)
            
            # Compute the inverse indices
            inverse_indices = dsa.map_blocks(map_to_indices, labels_dask, unique_values, 
                                        dtype=int, chunks=labels_dask.chunks)
            
            inverse_indices = inverse_indices.reshape(labels.shape)
            
            # Convert back to DataArray, preserving the original coordinates and attributes
            inverse_indices_da = xr.DataArray(inverse_indices, coords=labels.coords, dims=labels.dims, attrs=labels.attrs)
            
            return inverse_indices_da
        
        # temp_dask = dsa.unique(labels_wrapped.data, return_inverse=True)[1].reshape(labels_wrapped.shape)
        # labels_wrapped_unique = xr.DataArray(temp_dask, coords=labels_wrapped.coords, dims=labels_wrapped.dims, attrs=labels_wrapped.attrs)  
        labels_wrapped_unique = inverse_unique(labels_wrapped)  #(?)
        

        # Recalculate the total number of labels 
        N = labels_wrapped_unique.max() #np.max(labels_wrapped_unique)

        return labels_wrapped_unique, N

