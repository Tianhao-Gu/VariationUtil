class SampleToTrait:
    def __init__(self, callback_url, scratch):
        self.scratch = scratch
        self.dfu = DataFileUtil(callback_url)

    def import_trait(self, ctx, params):

        return 'test'